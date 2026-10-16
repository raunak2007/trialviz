"""End-to-end execution of one query: interpret, retrieve, aggregate, assemble, validate."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime

from trialviz.analysis.aggregate import group_by, order_buckets
from trialviz.analysis.dimensions import CODE_LABELS, MISSING, spec_for
from trialviz.ctgov.client import CallLog, CTGovClient, CTGovError
from trialviz.ctgov.exact_counts import EXACT_COUNT_DIMENSIONS, bucket_expressions, exact_counts
from trialviz.ctgov.records import PHASE_NOT_SPECIFIED, Trial, parse_studies
from trialviz.interpret.planner import Planner
from trialviz.schemas.enums import AnalysisType, Dimension, Measure
from trialviz.schemas.plan import EntityFilter, QueryPlan
from trialviz.schemas.request import QueryRequest
from trialviz.schemas.response import Meta, QueryResponse, SeriesRetrieval, SourceInfo, Timing
from trialviz.viz.build import SeriesData, build_visualization
from trialviz.viz.validate import check_spec

log = logging.getLogger(__name__)

# Only sampled dimensions with a per-trial category can gain a remainder bucket from exact counts.
REMAINDER_DIMENSIONS = {Dimension.PHASE, Dimension.STATUS, Dimension.STUDY_TYPE, Dimension.SPONSOR_CLASS}


class NoDataError(Exception):
    def __init__(self, message: str, filters: list[EntityFilter]):
        super().__init__(message)
        self.message = message
        self.filters = filters


@dataclass
class Pipeline:
    client: CTGovClient
    planner: Planner

    async def run(self, request: QueryRequest, *, today: date | None = None) -> QueryResponse:
        t0 = time.monotonic()
        log_calls = CallLog()

        outcome = await self.planner.plan(request.query, request.as_filter(), mode=request.planner, today=today)
        plan = outcome.plan
        t1 = time.monotonic()

        series, retrieval_warnings = await self._retrieve(plan, request, log_calls)
        t2 = time.monotonic()
        if sum(len(s.trials) for s in series) == 0:
            raise NoDataError(
                "No trials on ClinicalTrials.gov matched the filters. Check the spelling of the drug or condition, "
                "or loosen the phase, status, or year constraints.",
                [f for _, f in plan.resolved_filters()],
            )

        built = build_visualization(
            plan, series, include_citations=request.include_citations, citations_per_datum=request.citations_per_datum
        )
        fetched_ids = {t.nct_id for s in series for t in s.trials}
        check_spec(built.spec, fetched_ids, expected_sum=built.expected_sum)
        t3 = time.monotonic()

        meta = Meta(
            query=request.query,
            interpretation=plan.interpretation,
            assumptions=plan.assumptions + built.notes,
            warnings=retrieval_warnings + built.warnings,
            plan=plan,
            filters=plan.filter,
            retrieval=[
                SeriesRetrieval(
                    label=s.label,
                    filter=s.filter,
                    total_matching=s.total_matching,
                    fetched=len(s.trials),
                    truncated=s.truncated,
                    count_method=s.count_method,
                )
                for s in series
            ],
            source=SourceInfo(api_base=self.client.base_url, retrieved_at=datetime.now(UTC).isoformat(timespec="seconds")),
            api_calls=log_calls.calls,
            planner=outcome.info,
            timing=Timing(
                interpret_ms=int((t1 - t0) * 1000),
                retrieve_ms=int((t2 - t1) * 1000),
                aggregate_ms=int((t3 - t2) * 1000),
                total_ms=int((t3 - t0) * 1000),
            ),
        )
        return QueryResponse(visualization=built.spec, meta=meta)

    # ------------------------------------------------------------------ retrieval

    async def _retrieve(self, plan: QueryPlan, request: QueryRequest, log_calls: CallLog) -> tuple[list[SeriesData], list[str]]:
        """Fetch records for every series concurrently, then add exact counts where they are warranted."""
        resolved = plan.resolved_filters()

        async def fetch_one(label: str, f: EntityFilter) -> SeriesData:
            result = await self.client.fetch_studies(f, max_records=request.max_trials, log=log_calls)
            trials: list[Trial] = parse_studies(result.studies)
            return SeriesData(label=label, filter=f, trials=trials, total_matching=result.total_count, truncated=result.truncated)

        series = list(await asyncio.gather(*(fetch_one(label, f) for label, f in resolved)))
        warnings: list[str] = []

        count_dimension = self._exact_count_dimension(plan)
        if count_dimension is None:
            return series, warnings
        for s in series:
            wants_exact = request.count_mode == "exact" or (request.count_mode == "auto" and s.truncated)
            if not wants_exact or not s.trials:
                continue
            try:
                counts = await self._exact_counts_for(s, plan, count_dimension, log_calls)
            except CTGovError as exc:
                log.warning("exact counts failed for series %r: %s", s.label, exc)
                prefix = f"Series '{s.label}': " if len(series) > 1 else ""
                warnings.append(
                    f"{prefix}exact per-category counts could not be fetched ({exc}); values come from the fetched sample."
                )
                continue
            s.exact_counts = counts
            s.count_method = "exact_server_counts"
        return series, warnings

    @staticmethod
    def _exact_count_dimension(plan: QueryPlan) -> Dimension | None:
        """The dimension exact counts would apply to, or None when they do not make sense."""
        if plan.measure != Measure.TRIAL_COUNT or plan.split_by is not None:
            return None
        if plan.analysis == AnalysisType.TIME_TREND:
            return Dimension.START_YEAR
        if plan.analysis in (AnalysisType.DISTRIBUTION, AnalysisType.COMPARISON, AnalysisType.GEOGRAPHIC):
            return plan.dimension if plan.dimension in EXACT_COUNT_DIMENSIONS else None
        return None

    async def _exact_counts_for(self, s: SeriesData, plan: QueryPlan, dimension: Dimension, log_calls: CallLog) -> dict[str, int]:
        spec = spec_for(dimension)
        candidates: list[str] = []
        if dimension == Dimension.COUNTRY:
            # The sample tells us which countries are worth counting exactly.
            grouped = group_by(s.trials, spec, Measure.TRIAL_COUNT)
            ordered, _ = order_buckets(grouped.buckets, spec, plan.top_n or spec.default_top_n)
            candidates = [b.label for b in ordered]
        expressions = bucket_expressions(
            dimension,
            start_year=s.filter.start_year,
            end_year=s.filter.end_year,
            granularity=plan.time_granularity,
            candidates=candidates,
        )
        if not expressions:
            raise CTGovError("no server-side expressions for this dimension")
        count_filter = s.filter
        if dimension == Dimension.START_YEAR:
            # Each period clause already bounds the dates; drop the overall range so it is not stated twice.
            count_filter = s.filter.model_copy(update={"start_year": None, "end_year": None})
        raw = await exact_counts(self.client, count_filter, expressions, log=log_calls)

        labels = CODE_LABELS.get(dimension, {})
        counts = {labels.get(label, label): n for label, n in raw.items()}
        if dimension in REMAINDER_DIMENSIONS and s.total_matching is not None:
            remainder = s.total_matching - sum(counts.values())
            if remainder > 0:
                counts[PHASE_NOT_SPECIFIED if dimension == Dimension.PHASE else MISSING] = remainder
        return counts
