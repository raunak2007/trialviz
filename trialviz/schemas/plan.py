"""Query plans.

`PlanDraft` is the only thing the language model is allowed to produce. It is a
closed menu: every field is an enum, a bounded number, or a short list of
strings copied from the user's question. The model never sees trial data and
never emits a number that ends up in a chart.

`QueryPlan` is the normalized, validated form the retrieval and aggregation
layers execute. It is also echoed back in the response metadata so a client can
see exactly how the question was interpreted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trialviz.schemas.enums import (
    AnalysisType,
    Dimension,
    Measure,
    NetworkKind,
    NumericField,
    OverallStatus,
    Phase,
    StudyType,
    TimeGranularity,
    VizType,
)


class EntityFilter(BaseModel):
    """Constraints that narrow which trials are retrieved.

    Values inside one list are OR-ed, different fields are AND-ed. Strings are
    passed to the ClinicalTrials.gov search areas, which apply their own synonym
    expansion (for example brand names resolve to the generic drug).
    """

    model_config = ConfigDict(extra="forbid")

    drugs: list[str] = Field(default_factory=list, description="Intervention names, e.g. ['pembrolizumab']")
    conditions: list[str] = Field(default_factory=list, description="Conditions or diseases, e.g. ['melanoma']")
    sponsors: list[str] = Field(default_factory=list, description="Sponsor or collaborator organization names")
    countries: list[str] = Field(default_factory=list, description="Countries where the trial has a site")
    terms: list[str] = Field(
        default_factory=list,
        description="Free-text terms searched across all study fields; use only when a term is clearly neither a drug nor a condition",
    )
    phases: list[Phase] = Field(default_factory=list)
    statuses: list[OverallStatus] = Field(default_factory=list)
    study_types: list[StudyType] = Field(default_factory=list)
    start_year: int | None = Field(None, ge=1900, le=2100, description="Earliest trial start year (inclusive)")
    end_year: int | None = Field(None, ge=1900, le=2100, description="Latest trial start year (inclusive)")

    def is_empty(self) -> bool:
        return not any(
            [
                self.drugs,
                self.conditions,
                self.sponsors,
                self.countries,
                self.terms,
                self.phases,
                self.statuses,
                self.study_types,
                self.start_year is not None,
                self.end_year is not None,
            ]
        )

    def has_entity(self) -> bool:
        """True when at least one text search term is present.

        Without one the query would match the whole registry, which is never
        what a question is about and would make sampled counts meaningless.
        """
        return any([self.drugs, self.conditions, self.sponsors, self.countries, self.terms])

    def merged_over(self, base: EntityFilter) -> EntityFilter:
        """Overlay this filter on top of `base`: non-empty fields here win."""
        data = base.model_dump()
        for key, value in self.model_dump().items():
            if value not in (None, []):
                data[key] = value
        return EntityFilter.model_validate(data)


class Series(BaseModel):
    """One line or bar group in a comparison, e.g. 'Pembrolizumab' vs 'Nivolumab'."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., description="Short human label, e.g. the drug or condition name")
    filter: EntityFilter = Field(
        default_factory=EntityFilter,
        description="Filter overlaid on the base filter; only the fields that differ need to be set",
    )


class PlanDraft(BaseModel):
    """Structured interpretation of the user's question, produced by the planner.

    This is the schema the language model fills in. Keep it closed and boring.
    """

    model_config = ConfigDict(extra="forbid")

    analysis: AnalysisType = Field(..., description="The kind of question being asked")
    filter: EntityFilter = Field(default_factory=EntityFilter, description="Base filter shared by all series")
    dimension: Dimension | None = Field(
        None,
        description="Categorical field to group by (distribution, comparison, or split for a time trend)",
    )
    split_by: Dimension | None = Field(
        None,
        description="Optional secondary grouping: splits a time trend into several lines, a distribution "
        "into grouped bars, or colors scatter points",
    )
    measure: Measure = Field(Measure.TRIAL_COUNT, description="What each bar or point measures")
    series: list[Series] = Field(
        default_factory=list,
        description="For comparisons only: two to four entities compared side by side",
    )
    network: NetworkKind | None = Field(None, description="For network analyses: which two entity kinds to connect")
    numeric_x: NumericField | None = Field(None, description="Scatter x axis, or the binned field for a histogram")
    numeric_y: NumericField | None = Field(None, description="Scatter y axis")
    time_granularity: TimeGranularity = Field(TimeGranularity.YEAR, description="Bucket size for time trends")
    top_n: int | None = Field(None, ge=1, le=100, description="Keep only the N largest categories, nodes, or edges")
    interpretation: str = Field(..., description="One sentence restating how the question was understood")
    assumptions: list[str] = Field(
        default_factory=list,
        description="Short notes on choices made where the question was ambiguous",
    )
    unsupported_reason: str | None = Field(
        None, description="Set only when analysis is 'unsupported': why the question cannot be answered"
    )


class QueryPlan(BaseModel):
    """Validated, fully resolved plan. Produced by `trialviz.interpret.normalize`."""

    model_config = ConfigDict(extra="forbid")

    analysis: AnalysisType
    viz_type: VizType
    filter: EntityFilter
    series: list[Series] = Field(..., min_length=1, description="Always at least one series; comparisons have 2+")
    dimension: Dimension | None = None
    split_by: Dimension | None = None
    measure: Measure = Measure.TRIAL_COUNT
    network: NetworkKind | None = None
    numeric_x: NumericField | None = None
    numeric_y: NumericField | None = None
    time_granularity: TimeGranularity = TimeGranularity.YEAR
    top_n: int | None = None
    interpretation: str
    assumptions: list[str] = Field(default_factory=list)
    planner_mode: Literal["llm", "rules"]

    @model_validator(mode="after")
    def _check_shape(self) -> QueryPlan:
        a = self.analysis
        if a == AnalysisType.UNSUPPORTED:
            raise ValueError("an unsupported analysis cannot become an executable plan")
        if a == AnalysisType.COMPARISON and len(self.series) < 2:
            raise ValueError("a comparison needs at least two series")
        if a in (AnalysisType.DISTRIBUTION, AnalysisType.COMPARISON) and self.dimension is None:
            raise ValueError(f"{a.value} needs a dimension")
        if a == AnalysisType.NETWORK and self.network is None:
            raise ValueError("a network analysis needs a network kind")
        if a == AnalysisType.SCATTER and (self.numeric_x is None or self.numeric_y is None):
            raise ValueError("a scatter plot needs numeric_x and numeric_y")
        if a == AnalysisType.HISTOGRAM and self.numeric_x is None:
            raise ValueError("a histogram needs numeric_x")
        for s in self.series:
            if not s.filter.merged_over(self.filter).has_entity():
                raise ValueError(f"series '{s.label}' has no drug, condition, sponsor, or country to search for")
        return self

    def resolved_filters(self) -> list[tuple[str, EntityFilter]]:
        """(label, effective filter) for each series, with the base filter applied."""
        return [(s.label, s.filter.merged_over(self.filter)) for s in self.series]
