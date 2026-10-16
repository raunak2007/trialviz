"""Turn a PlanDraft into an executable QueryPlan.

This is where defaults, structured-field precedence, and sanity rules live.
Keeping them here (rather than in the prompt) means they apply identically to
model-produced and rule-produced drafts, and they are unit-testable.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import ValidationError

from trialviz.analysis.dimensions import spec_for
from trialviz.schemas.enums import AnalysisType, Dimension, Measure, NetworkKind, NumericField, StudyType, VizType
from trialviz.schemas.plan import EntityFilter, PlanDraft, QueryPlan, Series
from trialviz.viz.titles import entity_phrase

DEFAULT_TREND_YEARS = 10

SUGGESTIONS = [
    "How has the number of trials for pembrolizumab changed per year since 2015?",
    "How are melanoma trials distributed across phases?",
    "Compare phases for trials involving nivolumab vs ipilimumab.",
    "Which countries have the most recruiting trials for type 2 diabetes?",
    "Show a network of sponsors and drugs for melanoma trials.",
    "Which drugs frequently co-occur in combination studies for lung cancer?",
    "Plot enrollment against start year for breast cancer trials.",
]


class PlanError(Exception):
    """The question could not be turned into a plan. Carries a client-facing code."""

    def __init__(
        self, code: Literal["needs_clarification", "unsupported_query"], message: str, suggestions: list[str] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestions = suggestions or []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        cleaned = " ".join(v.split())
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def clean_filter(f: EntityFilter) -> EntityFilter:
    data = f.model_dump()
    for key in ("drugs", "conditions", "sponsors", "countries", "terms"):
        data[key] = _dedupe(data[key])
    for key in ("phases", "statuses", "study_types"):
        data[key] = list(dict.fromkeys(data[key]))
    return EntityFilter.model_validate(data)


def normalize(
    draft: PlanDraft, structured: EntityFilter, *, mode: Literal["llm", "rules"], today: date | None = None
) -> QueryPlan:
    """Resolve defaults and precedence, then validate. Raises PlanError when the question is not answerable."""
    today = today or date.today()
    assumptions = list(draft.assumptions)

    if draft.analysis == AnalysisType.UNSUPPORTED:
        raise PlanError(
            "unsupported_query",
            draft.unsupported_reason or "This question does not map to a supported analysis.",
            SUGGESTIONS,
        )

    # Structured request fields are authoritative over anything read from the question.
    base = clean_filter(clean_filter(structured).merged_over(draft.filter))

    analysis = draft.analysis
    dimension = draft.dimension
    split_by = draft.split_by
    measure = draft.measure
    network = draft.network
    numeric_x, numeric_y = draft.numeric_x, draft.numeric_y
    top_n = draft.top_n

    # A distribution over start year is a time trend; a time trend with a dimension means "split by it".
    if analysis == AnalysisType.DISTRIBUTION and dimension == Dimension.START_YEAR:
        analysis = AnalysisType.TIME_TREND
        dimension = None
    if analysis == AnalysisType.TIME_TREND and dimension not in (None, Dimension.START_YEAR):
        split_by = split_by or dimension
        dimension = None
    if analysis == AnalysisType.GEOGRAPHIC:
        dimension = Dimension.COUNTRY
        top_n = top_n or spec_for(Dimension.COUNTRY).default_top_n
    if analysis == AnalysisType.COMPARISON and dimension is None:
        dimension = Dimension.PHASE
        assumptions.append("Compared the entities by phase because the question did not say what to compare.")
    if analysis == AnalysisType.NETWORK and network is None:
        network = NetworkKind.SPONSOR_DRUG
        assumptions.append("Built a sponsor-to-drug network because the question did not name the entities to connect.")
    if analysis == AnalysisType.SCATTER:
        if numeric_x is None or numeric_y is None:
            numeric_x, numeric_y = NumericField.START_YEAR, NumericField.ENROLLMENT
            assumptions.append("Plotted enrollment against start year because the axes were not specified.")
        if numeric_x == numeric_y:
            raise PlanError("needs_clarification", "A scatter plot needs two different numeric fields.", SUGGESTIONS)
    if analysis == AnalysisType.HISTOGRAM and numeric_x is None:
        numeric_x = NumericField.ENROLLMENT
        assumptions.append("Binned trial enrollment because the question did not name a numeric field.")
    if split_by is not None and split_by == dimension:
        split_by = None
    if analysis == AnalysisType.COMPARISON and split_by is not None:
        assumptions.append(f"Ignored the secondary grouping by {split_by.value}: comparisons already use color for the series.")
        split_by = None
    if measure == Measure.TOTAL_ENROLLMENT and analysis in (AnalysisType.NETWORK, AnalysisType.SCATTER, AnalysisType.HISTOGRAM):
        measure = Measure.TRIAL_COUNT

    # Phases only exist for interventional studies; observational ones would all land in "Not specified".
    uses_phase = Dimension.PHASE in (dimension, split_by)
    if uses_phase and not base.study_types:
        base = base.model_copy(update={"study_types": [StudyType.INTERVENTIONAL]})
        assumptions.append("Restricted to interventional studies because observational studies do not have a phase.")

    # Time trends need a closed range so periods can be enumerated and counted exactly.
    if analysis == AnalysisType.TIME_TREND or (analysis == AnalysisType.COMPARISON and dimension == Dimension.START_YEAR):
        if base.start_year is None and base.end_year is None:
            base = base.model_copy(update={"start_year": today.year - DEFAULT_TREND_YEARS, "end_year": today.year})
            assumptions.append(
                f"Defaulted the time window to the last {DEFAULT_TREND_YEARS} years ({base.start_year} to {base.end_year})."
            )
        elif base.end_year is None:
            base = base.model_copy(update={"end_year": today.year})
        elif base.start_year is None:
            base = base.model_copy(update={"start_year": base.end_year - DEFAULT_TREND_YEARS})
            assumptions.append(f"Started the time window {DEFAULT_TREND_YEARS} years before {base.end_year}.")
    if base.start_year is not None and base.end_year is not None and base.end_year < base.start_year:
        raise PlanError("needs_clarification", f"The year range {base.start_year} to {base.end_year} is reversed.")

    # Series: comparisons carry their own; everything else is one series over the base filter.
    if analysis == AnalysisType.COMPARISON:
        series = [
            Series(label=s.label.strip() or f"Series {i + 1}", filter=clean_filter(s.filter)) for i, s in enumerate(draft.series)
        ]
        if len(series) < 2:
            raise PlanError(
                "needs_clarification",
                "A comparison needs at least two things to compare, for example 'drug A vs drug B'.",
                SUGGESTIONS,
            )
        resolved = [s.filter.merged_over(base).model_dump() for s in series]
        if any(resolved[i] == resolved[j] for i in range(len(resolved)) for j in range(i + 1, len(resolved))):
            raise PlanError(
                "needs_clarification",
                "The compared series resolve to the same filter; each side of a comparison needs its own drug, condition, or sponsor.",
                SUGGESTIONS,
            )
    else:
        series = [Series(label=entity_phrase(base) if base.has_entity() else "all trials")]

    try:
        return QueryPlan(
            analysis=analysis,
            viz_type=_viz_type(analysis, dimension, split_by, len(series)),
            filter=base,
            series=series,
            dimension=dimension,
            split_by=split_by,
            measure=measure,
            network=network,
            numeric_x=numeric_x,
            numeric_y=numeric_y,
            time_granularity=draft.time_granularity,
            top_n=top_n,
            interpretation=draft.interpretation.strip() or "Interpreted the question with default settings.",
            assumptions=assumptions,
            planner_mode=mode,
        )
    except ValidationError as exc:
        messages = "; ".join(e.get("msg", "") for e in exc.errors())
        if "no drug, condition" in messages:
            raise PlanError(
                "needs_clarification",
                "Which drug, condition, sponsor, or country should the trials be limited to? Add it to the question or pass a structured field.",
                SUGGESTIONS,
            ) from exc
        raise PlanError("needs_clarification", f"The question could not be turned into a plan: {messages}", SUGGESTIONS) from exc


def _viz_type(analysis: AnalysisType, dimension: Dimension | None, split_by: Dimension | None, n_series: int) -> VizType:
    if analysis == AnalysisType.TIME_TREND or (analysis == AnalysisType.COMPARISON and dimension == Dimension.START_YEAR):
        return VizType.TIME_SERIES
    if analysis in (AnalysisType.DISTRIBUTION, AnalysisType.GEOGRAPHIC):
        return VizType.GROUPED_BAR_CHART if split_by else VizType.BAR_CHART
    if analysis == AnalysisType.COMPARISON:
        return VizType.GROUPED_BAR_CHART
    if analysis == AnalysisType.NETWORK:
        return VizType.NETWORK_GRAPH
    if analysis == AnalysisType.SCATTER:
        return VizType.SCATTER_PLOT
    if analysis == AnalysisType.HISTOGRAM:
        return VizType.HISTOGRAM
    raise ValueError(f"no visualization for {analysis}")
