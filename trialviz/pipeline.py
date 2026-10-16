"""End-to-end execution of one query: interpret, retrieve, aggregate, assemble, verify."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import combinations

from trialviz.analysis.aggregate import group_by, order_buckets
from trialviz.analysis.dimensions import CODE_LABELS, MISSING, spec_for
from trialviz.ctgov.client import CallLog, CTGovClient, CTGovError
from trialviz.ctgov.exact_counts import EXACT_COUNT_DIMENSIONS, bucket_expressions, exact_counts
from trialviz.ctgov.records import PHASE_NOT_SPECIFIED, Trial, parse_studies
from trialviz.interpret.planner import Planner
from trialviz.schemas.enums import AnalysisType, Dimension, Measure
from trialviz.schemas.plan import EntityFilter, QueryPlan
from trialviz.schemas.request import QueryRequest
from trialviz.schemas.response import DrugMatch, Meta, QueryResponse, SeriesOverlap, SeriesRetrieval, SourceInfo, Timing
from trialviz.viz.build import FetchPolicy, SeriesData, build_visualization
from trialviz.viz.validate import verify_spec

log = logging.getLogger(__name__)

# Only sampled dimensions with a per-trial category can gain a remainder bucket from exact counts.
REMAINDER_DIMENSIONS = {Dimension.PHASE, Dimension.STATUS, Dimension.STUDY_TYPE, Dimension.SPONSOR_CLASS}

# Rankings over open vocabularies: the categories themselves come from the records, so a sample of the
# most recent trials would rank the wrong things. These are fetched completely up to the ceiling.
OPEN_VOCABULARY_DIMENSIONS = {Dimension.SPONSOR, Dimension.CONDITION, Dimension.INTERVENTION, Dimension.COUNTRY}


def plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


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

        series, retrieval_warnings, retrieval_notes = await self._retrieve(plan, request, log_calls)
        t2 = time.monotonic()
        if sum(len(s.trials) for s in series) == 0:
            raise NoDataError(
                "No trials on ClinicalTrials.gov matched the filters. Check the spelling of the drug or condition, "
                "or loosen the phase, status, or year constraints.",
                [f for _, f in plan.resolved_filters()],
            )

        overlaps, overlap_warnings = _series_overlap(plan, series, request.comparison_mode)
        chart_series, disjoint, partition_notes, partition_warnings = _partition_if_requested(plan, series, request)
        built = build_visualization(
            plan,
            chart_series,
            include_citations=request.include_citations,
            citations_per_datum=request.citations_per_datum,
            series_disjoint=disjoint,
        )
        verification = verify_spec(built.spec, plan, chart_series)
        t3 = time.monotonic()

        meta = Meta(
            query=request.query,
            interpretation=plan.interpretation,
            assumptions=plan.assumptions + retrieval_notes + partition_notes + built.notes,
            warnings=retrieval_warnings + overlap_warnings + partition_warnings + built.warnings,
            plan=plan,
            filters=plan.filter,
            retrieval=[_retrieval_info(s) for s in series],
            series_overlap=overlaps,
            verification=verification,
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

    async def _retrieve(
        self, plan: QueryPlan, request: QueryRequest, log_calls: CallLog
    ) -> tuple[list[SeriesData], list[str], list[str]]:
        """Fetch records for every series concurrently, then add exact counts where they are warranted."""
        resolved = plan.resolved_filters()
        policy = fetch_policy(plan, request)
        ceiling = request.complete_up_to if policy == "complete" else None

        async def fetch_one(label: str, f: EntityFilter) -> SeriesData:
            result = await self.client.fetch_studies(f, max_records=request.max_trials, complete_up_to=ceiling, log=log_calls)
            trials: list[Trial] = parse_studies(result.studies)
            return SeriesData(
                label=label,
                filter=f,
                trials=trials,
                total_matching=result.total_count,
                truncated=result.truncated,
                fetch_policy=policy,
            )

        series = list(await asyncio.gather(*(fetch_one(label, f) for label, f in resolved)))
        warnings: list[str] = []
        notes: list[str] = []
        many = len(series) > 1

        for s in series:
            prefix = f"Series '{s.label}': " if many else ""
            if policy == "complete" and not s.truncated and s.total_matching is not None:
                notes.append(
                    f"{prefix}all {s.total_matching:,} matching {plural(s.total_matching, 'trial')} were fetched; values are exact."
                )
            if policy == "complete" and s.truncated and s.total_matching is not None:
                warnings.append(
                    f"{prefix}{s.total_matching:,} trials match, above the complete_up_to ceiling of "
                    f"{request.complete_up_to:,}; the chart is built from the {len(s.trials):,} most recently started "
                    "trials and its values are marked sampled. Raise complete_up_to (up to 20,000) or narrow the filter."
                )
            if s.filter.drugs:
                note = _apply_drug_match(s, request.drug_match, prefix)
                if note:
                    notes.append(note)

        count_dimension = self._exact_count_dimension(plan) if request.drug_match == "any" else None
        if count_dimension is None:
            return series, warnings, notes
        for s in series:
            wants_exact = request.count_mode == "exact" or (request.count_mode == "auto" and s.truncated)
            if not wants_exact or not s.trials:
                continue
            try:
                counts = await self._exact_counts_for(s, plan, count_dimension, log_calls)
            except CTGovError as exc:
                log.warning("exact counts failed for series %r: %s", s.label, exc)
                prefix = f"Series '{s.label}': " if many else ""
                warnings.append(
                    f"{prefix}exact per-category counts could not be fetched ({exc}); values come from the fetched sample."
                )
                continue
            s.exact_counts = counts
            s.count_method = "exact_server_counts"
        return series, warnings, notes

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


# ---------------------------------------------------------------------- policies


def fetch_policy(plan: QueryPlan, request: QueryRequest) -> FetchPolicy:
    """'complete' for analyses whose answer depends on which records were fetched, 'sample' when registry
    counts can stand in for the records that were not."""
    a = plan.analysis
    if a in (AnalysisType.NETWORK, AnalysisType.SCATTER, AnalysisType.HISTOGRAM, AnalysisType.GEOGRAPHIC):
        return "complete"
    if plan.dimension in OPEN_VOCABULARY_DIMENSIONS or plan.split_by is not None:
        return "complete"
    if plan.measure == Measure.TOTAL_ENROLLMENT:
        return "complete"
    if a == AnalysisType.COMPARISON and request.comparison_mode == "partition":
        return "complete"
    if request.drug_match == "strict":
        return "complete"
    return "sample"


def _apply_drug_match(s: SeriesData, mode: str, prefix: str) -> str | None:
    """Measure how many fetched trials list the searched drug as an intervention; drop the rest under 'strict'."""
    listed = [t for t in s.trials if any(t.mentions_drug(d) for d in s.filter.drugs)]
    mention_only = len(s.trials) - len(listed)
    s.drug_match = DrugMatch(mode=mode, listed_as_intervention=len(listed), mention_only=mention_only)  # type: ignore[arg-type]
    if mention_only == 0:
        return None
    drugs = " or ".join(s.filter.drugs)
    if mode == "strict":
        s.excluded_mention_only = mention_only
        s.trials = listed
        return (
            f"{prefix}{mention_only:,} of {mention_only + len(listed):,} fetched trials matched '{drugs}' without listing it "
            "as an intervention (the registry's intervention search also matches descriptions); drug_match=strict "
            "excluded them from the chart."
        )
    return (
        f"{prefix}{mention_only:,} of {len(s.trials):,} fetched trials matched '{drugs}' without listing it as an "
        "intervention by name, other name, or MeSH term (the registry's intervention search also matches descriptions); "
        "they are included. Set drug_match=strict to exclude them."
    )


def _retrieval_info(s: SeriesData) -> SeriesRetrieval:
    fetched = len(s.trials) + s.excluded_mention_only
    if s.total_matching:
        coverage = round(min(fetched / s.total_matching, 1.0), 4)
    else:
        coverage = 0.0 if s.truncated else 1.0
    return SeriesRetrieval(
        label=s.label,
        filter=s.filter,
        total_matching=s.total_matching,
        fetched=fetched,
        truncated=s.truncated,
        coverage=coverage,
        fetch_policy=s.fetch_policy,
        count_method=s.count_method,
        drug_match=s.drug_match,
    )


# ---------------------------------------------------------------------- comparisons


def _series_overlap(plan: QueryPlan, series: list[SeriesData], mode: str) -> tuple[list[SeriesOverlap], list[str]]:
    """Trials that belong to more than one series of a comparison, reported as data and as a warning."""
    if plan.analysis != AnalysisType.COMPARISON or len(series) < 2:
        return [], []
    exact = all(not s.truncated for s in series)
    overlaps: list[SeriesOverlap] = []
    warnings: list[str] = []
    groups = list(combinations(series, 2))
    if len(series) > 2:
        groups.append(tuple(series))  # type: ignore[arg-type]
    for group in groups:
        common = set.intersection(*(s.trial_ids for s in group))
        overlaps.append(SeriesOverlap(series=[s.label for s in group], trials_in_all=len(common), exact=exact))
    if mode == "overlapping":
        for o in overlaps:
            if o.trials_in_all == 0 or len(o.series) != 2:
                continue
            qualifier = "" if exact else "at least "
            a, b = o.series
            warnings.append(
                f"{qualifier}{o.trials_in_all:,} {plural(o.trials_in_all, 'trial')} involve{'s' if o.trials_in_all == 1 else ''} "
                f"both '{a}' and '{b}' and {'is' if o.trials_in_all == 1 else 'are'} counted in both series, so values "
                "across series do not add up "
                "to distinct trials. comparison_mode=partition counts each trial once."
            )
    return overlaps, warnings


def _partition_if_requested(
    plan: QueryPlan, series: list[SeriesData], request: QueryRequest
) -> tuple[list[SeriesData], bool, list[str], list[str]]:
    """Regroup a comparison into 'A only', 'B only', and 'Both' so no trial is counted twice."""
    if plan.analysis != AnalysisType.COMPARISON or request.comparison_mode != "partition" or len(series) < 2:
        return series, False, [], []
    if any(s.truncated for s in series):
        labels = ", ".join(f"'{s.label}'" for s in series if s.truncated)
        return (
            series,
            False,
            [],
            [
                f"comparison_mode=partition needs every record of every series, but {labels} exceeded the complete_up_to "
                f"ceiling of {request.complete_up_to:,}; showing overlapping series instead."
            ],
        )
    ids = {s.label: s.trial_ids for s in series}
    by_id = {t.nct_id: t for s in series for t in s.trials}
    groups: list[SeriesData] = []
    for s in series:
        others = set().union(*(ids[o.label] for o in series if o is not s))
        only = ids[s.label] - others
        groups.append(_group(f"{s.label} only", s.filter, [by_id[i] for i in sorted(only)], s.fetch_policy))
    shared = {i for i in by_id if sum(i in ids[s.label] for s in series) > 1}
    shared_label = "Both" if len(series) == 2 else "More than one"
    groups.append(_group(shared_label, plan.filter, [by_id[i] for i in sorted(shared)], series[0].fetch_policy))
    summary = ", ".join(f"'{g.label}' ({len(g.trials):,})" for g in groups)
    note = f"comparison_mode=partition: trials were regrouped into {summary} so each trial is counted once."
    return groups, True, [note], []


def _group(label: str, f: EntityFilter, trials: list[Trial], policy: FetchPolicy) -> SeriesData:
    return SeriesData(label=label, filter=f, trials=trials, total_matching=len(trials), truncated=False, fetch_policy=policy)
