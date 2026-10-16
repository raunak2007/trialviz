"""Response schema for POST /query.

The visualization is a discriminated union on `type`. A renderer switches on
`type`, reads the field names from `encoding`, and looks those keys up in each
row of `data`. Row keys are documented inline in `columns`, so nothing has to
be guessed. Every row, node, and edge can carry `citations` back to the trial
records that produced it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from trialviz.schemas.enums import TimeGranularity
from trialviz.schemas.plan import EntityFilter, QueryPlan

# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """One field/value pair copied verbatim from a ClinicalTrials.gov API record."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., description="JSON path within the API study record, e.g. protocolSection.designModule.phases")
    value: str = Field(..., description="Exact value at that path. Lists and objects are JSON-encoded.")


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nct_id: str = Field(..., description="Registry identifier, e.g. NCT01234567")
    title: str | None = Field(None, description="Brief title of the trial")
    url: str = Field(..., description="Public record URL on ClinicalTrials.gov")
    evidence: list[Evidence] = Field(..., description="The field values that place this trial in this datum")


class Cited(BaseModel):
    """Mixin fields present on every row, node, and edge."""

    citations: list[Citation] = Field(
        default_factory=list,
        description="Up to `citations_per_datum` supporting trial records (empty when citations are disabled)",
    )
    citation_count: int = Field(..., ge=0, description="Number of distinct trials supporting this datum, before truncation")


# ---------------------------------------------------------------------------
# Encodings and column documentation
# ---------------------------------------------------------------------------

ChannelType = Literal["nominal", "ordinal", "quantitative", "temporal"]


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., description="Key to read from each data row")
    type: ChannelType = Field(..., description="Vega-Lite style measurement type")
    title: str = Field(..., description="Axis or legend title")
    granularity: TimeGranularity | None = Field(None, description="For temporal channels: bucket size of the values")


class ChartEncoding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: Channel
    y: Channel
    color: Channel | None = Field(None, description="Series / group channel, present for grouped and multi-line charts")
    tooltip: list[str] = Field(default_factory=list, description="Row keys worth showing on hover")


class NodeEncoding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field("id", description="Node key holding the unique id")
    label: str = Field("label", description="Node key holding the display label")
    group: str = Field("kind", description="Node key holding the node kind (used for color)")
    size: str = Field("trial_count", description="Node key holding the numeric size")


class EdgeEncoding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field("source", description="Edge key holding the source node id")
    target: str = Field("target", description="Edge key holding the target node id")
    weight: str = Field("trial_count", description="Edge key holding the numeric weight")


class NetworkEncoding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: NodeEncoding = Field(default_factory=NodeEncoding)
    edges: EdgeEncoding = Field(default_factory=EdgeEncoding)
    directed: bool = Field(False, description="Edges are undirected co-occurrences")


class Column(BaseModel):
    """Documents one key present on every data row of a chart."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["string", "integer", "number", "boolean"]
    description: str


# ---------------------------------------------------------------------------
# Data rows
# ---------------------------------------------------------------------------


class Row(Cited):
    """A chart data row. Besides the citation fields, keys are named by `encoding` and documented in `columns`."""

    model_config = ConfigDict(extra="allow")

    @model_serializer(mode="wrap")
    def _data_fields_first(self, handler: SerializerFunctionWrapHandler) -> dict:
        """Serialize the chart fields before the citation fields so rows read naturally."""
        data = handler(self)
        tail = {k: data.pop(k) for k in ("citation_count", "citations") if k in data}
        return {**data, **tail}


class Node(Cited):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    kind: str = Field(..., description="Entity kind, e.g. 'sponsor' or 'drug'")
    trial_count: int = Field(..., ge=0, description="Trials in which this entity appears")


class Edge(Cited):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    trial_count: int = Field(..., ge=1, description="Trials in which both endpoints appear together")


class NetworkData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node]
    edges: list[Edge]


# ---------------------------------------------------------------------------
# Visualization specs (discriminated on `type`)
# ---------------------------------------------------------------------------

SortOrder = Literal["natural", "value_desc", "value_asc"]


class ChartConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sort: SortOrder = Field("natural", description="Row order to preserve when drawing the x axis")
    stacked: bool = Field(False, description="For grouped bars: stack instead of placing side by side")
    value_format: Literal["integer", "number"] = Field("integer", description="Number format for the y channel")
    y_zero_based: bool = Field(True, description="Y axis should start at zero")


class BinSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., description="Per-trial numeric field that was binned")
    edges: list[float] = Field(..., description="Bin boundaries; bin i spans [edges[i], edges[i+1]). The last bin is open-ended.")
    unit: str | None = None


class _ChartBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    subtitle: str | None = Field(None, description="Filters and caveats in one line, suitable to show under the title")
    encoding: ChartEncoding
    columns: list[Column] = Field(..., description="Every key present on each data row")
    config: ChartConfig = Field(default_factory=ChartConfig)
    data: list[Row]


class BarChartSpec(_ChartBase):
    type: Literal["bar_chart"]


class GroupedBarChartSpec(_ChartBase):
    type: Literal["grouped_bar_chart"]


class TimeSeriesSpec(_ChartBase):
    type: Literal["time_series"]


class HistogramSpec(_ChartBase):
    type: Literal["histogram"]
    bins: BinSpec


class ScatterPlotSpec(_ChartBase):
    type: Literal["scatter_plot"]


class NetworkGraphSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["network_graph"]
    title: str
    subtitle: str | None = None
    encoding: NetworkEncoding = Field(default_factory=NetworkEncoding)
    node_kinds: list[str] = Field(..., description="Distinct values of the node `kind` field, in legend order")
    data: NetworkData


VisualizationSpec = Annotated[
    BarChartSpec | GroupedBarChartSpec | TimeSeriesSpec | HistogramSpec | ScatterPlotSpec | NetworkGraphSpec,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class ApiCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., description="Full request URL sent to ClinicalTrials.gov (reproducible with curl)")
    status: int
    elapsed_ms: int
    cached: bool = False
    studies_returned: int | None = None
    total_count: int | None = None


class SeriesRetrieval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    filter: EntityFilter = Field(..., description="Effective filter used for this series")
    total_matching: int | None = Field(None, description="Trials matching the filter according to the API")
    fetched: int = Field(..., description="Trial records actually downloaded and aggregated")
    truncated: bool = Field(..., description="True when fetched < total_matching")
    sample_order: str = Field(
        "StartDate:desc", description="Sort order used when fetching, so a truncated sample is well defined"
    )
    count_method: Literal["sampled", "exact_server_counts"] = Field(
        ..., description="'exact_server_counts' means bar heights came from one API count query per category"
    )


class PlannerInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["llm", "rules"]
    model: str | None = None
    attempts: int = 1
    fallback_reason: str | None = Field(None, description="Why the rule-based planner was used instead of the model")


class Timing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpret_ms: int
    retrieve_ms: int
    aggregate_ms: int
    total_ms: int


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "ClinicalTrials.gov"
    api_base: str = "https://clinicaltrials.gov/api/v2"
    retrieved_at: str = Field(..., description="ISO 8601 UTC timestamp")


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="The question as received")
    interpretation: str = Field(..., description="One sentence on how the question was read")
    assumptions: list[str] = Field(default_factory=list, description="Choices made where the question was ambiguous")
    warnings: list[str] = Field(default_factory=list, description="Data caveats the frontend should surface, e.g. truncation")
    plan: QueryPlan = Field(..., description="The executed plan, for transparency and debugging")
    filters: EntityFilter = Field(..., description="Base filter applied to every series")
    retrieval: list[SeriesRetrieval]
    source: SourceInfo
    api_calls: list[ApiCall]
    planner: PlannerInfo
    timing: Timing


class QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visualization: VisualizationSpec
    meta: Meta


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

ErrorCode = Literal["needs_clarification", "unsupported_query", "no_data", "upstream_error", "invalid_request", "internal_error"]


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    suggestions: list[str] = Field(
        default_factory=list, description="Rephrasings or structured fields that would make the query answerable"
    )
    details: dict | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
