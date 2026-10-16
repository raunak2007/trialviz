"""Assemble a VisualizationSpec from a plan and the trials retrieved per series.

Each analysis type has one builder. Builders only reshape aggregation output
into rows, encodings, and column docs; they never compute new numbers beyond
what `trialviz.analysis` produced. Besides the rows, every chart carries the
bookkeeping a reader needs to interpret the numbers: whether a trial can sit
in more than one row (`groupby_semantics`), how many distinct trials are
behind the rows, how many fetched trials landed nowhere, and per row whether
the value is exact or computed from a sample.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from trialviz.analysis.aggregate import Bucket, fill_period_gaps, group_by, group_by_pair, order_buckets
from trialviz.analysis.dimensions import MISSING, DimensionSpec, spec_for
from trialviz.analysis.networks import build_network
from trialviz.analysis.numeric import NUMERIC_TITLES, histogram, scatter_points
from trialviz.ctgov.records import Trial
from trialviz.schemas.enums import AnalysisType, Dimension, Measure
from trialviz.schemas.plan import EntityFilter, QueryPlan
from trialviz.schemas.response import (
    BarChartSpec,
    BinSpec,
    Channel,
    ChartConfig,
    ChartEncoding,
    Citation,
    Column,
    DrugMatch,
    Exactness,
    GroupbySemantics,
    GroupedBarChartSpec,
    HistogramSpec,
    NetworkData,
    NetworkGraphSpec,
    Row,
    ScatterPlotSpec,
    TimeSeriesSpec,
    VisualizationSpec,
)
from trialviz.viz.titles import subtitle_for, title_for

CountMethod = Literal["sampled", "exact_server_counts"]
FetchPolicy = Literal["sample", "complete"]


@dataclass
class SeriesData:
    """Everything retrieved for one series of the plan."""

    label: str
    filter: EntityFilter
    trials: list[Trial]
    total_matching: int | None
    truncated: bool
    count_method: CountMethod = "sampled"
    exact_counts: dict[str, int] | None = None  # category label -> exact API count (primary dimension only)
    fetch_policy: FetchPolicy = "sample"
    drug_match: DrugMatch | None = None
    excluded_mention_only: int = 0  # trials dropped by drug_match=strict

    @property
    def exactness(self) -> Exactness:
        """Values computed from this series' records are exact only when every matching record was fetched."""
        return "sampled" if self.truncated else "exact"

    @property
    def trial_ids(self) -> set[str]:
        return {t.nct_id for t in self.trials}


@dataclass
class BuildResult:
    spec: VisualizationSpec
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_visualization(
    plan: QueryPlan,
    series: list[SeriesData],
    *,
    include_citations: bool,
    citations_per_datum: int,
    series_disjoint: bool = False,
) -> BuildResult:
    """`series_disjoint` says the series were regrouped so that no trial belongs to more than one of them."""
    limit = citations_per_datum if include_citations else 0
    a = plan.analysis
    if a in (AnalysisType.DISTRIBUTION, AnalysisType.COMPARISON, AnalysisType.GEOGRAPHIC):
        if plan.dimension == Dimension.START_YEAR:
            return _build_time_series(plan, series, limit, series_disjoint)
        return _build_bars(plan, series, limit, series_disjoint)
    if a == AnalysisType.TIME_TREND:
        return _build_time_series(plan, series, limit, series_disjoint)
    if a == AnalysisType.NETWORK:
        return _build_network(plan, series, limit)
    if a == AnalysisType.SCATTER:
        return _build_scatter(plan, series, limit)
    if a == AnalysisType.HISTOGRAM:
        return _build_histogram(plan, series, limit)
    raise ValueError(f"no builder for analysis {a}")


# ---------------------------------------------------------------- helpers


def _measure_title(measure: Measure) -> str:
    return "Total enrollment" if measure == Measure.TOTAL_ENROLLMENT else "Number of trials"


def _sample_notes(series: list[SeriesData]) -> list[str]:
    notes: list[str] = []
    for s in series:
        prefix = f"Series '{s.label}': " if len(series) > 1 else ""
        if not s.truncated or s.total_matching is None:
            continue
        if s.count_method == "exact_server_counts":
            notes.append(
                f"{prefix}{s.total_matching:,} trials match; values are exact ClinicalTrials.gov counts per category, "
                f"citations come from the {len(s.trials):,} most recently started trials."
            )
        else:
            notes.append(
                f"{prefix}{s.total_matching:,} trials match but only the {len(s.trials):,} most recently started "
                f"were fetched; values are computed from that sample."
            )
    return notes


def _row(values: dict, bucket: Bucket | None, limit: int, exactness: Exactness, citation_count: int | None = None) -> Row:
    citations: list[Citation] = bucket.citations(limit) if bucket is not None and limit else []
    count = citation_count if citation_count is not None else (bucket.trial_count if bucket else 0)
    return Row(**values, exactness=exactness, citations=citations, citation_count=count)


def _bucket_exactness(bucket: Bucket | None, s: SeriesData) -> Exactness:
    if bucket is not None and bucket.exact:
        return "exact"
    return s.exactness


def _dimension_columns(spec: DimensionSpec, measure: Measure, with_series: bool, with_code: bool) -> list[Column]:
    cols = [Column(name=spec.key, type="string", description=f"{spec.title} (display label)")]
    if with_code:
        cols.append(
            Column(name=spec.code_key, type="string", description=f"Raw ClinicalTrials.gov value behind `{spec.key}`, or null")
        )
    if with_series:
        cols.append(Column(name="series", type="string", description="Series label (which entity the row belongs to)"))
    cols.append(Column(name=measure.value, type="integer", description=_measure_title(measure)))
    cols += _citation_columns()
    return cols


def _citation_columns() -> list[Column]:
    return [
        Column(name="exactness", type="string", description="'exact' or 'sampled' (see schema)"),
        Column(name="citation_count", type="integer", description="Distinct trials supporting this row"),
        Column(
            name="citations",
            type="string",
            description="Array of Citation objects (see schema), truncated to citations_per_datum",
        ),
    ]


def _apply_exact(buckets: list[Bucket], spec: DimensionSpec, exact: dict[str, int] | None) -> list[Bucket]:
    """Replace sampled values with exact API counts, adding buckets the sample missed."""
    if not exact:
        return buckets
    by_label = {b.label: b for b in buckets}
    for label, count in exact.items():
        bucket = by_label.get(label)
        if bucket is None:
            # Zero-count categories are left out of bar charts; periods are kept so the series has
            # one exact point per period.
            if count == 0 and spec.dimension != Dimension.START_YEAR:
                continue
            bucket = Bucket(label=label)
            if spec.dimension == Dimension.START_YEAR:
                year = label[:4]
                month = (
                    {"Q1": "01", "Q2": "04", "Q3": "07", "Q4": "10"}.get(label[5:], label[5:7] or "01")
                    if len(label) > 4
                    else "01"
                )
                bucket.period_start = f"{year}-{month}-01"
            by_label[label] = bucket
            buckets.append(bucket)
        bucket.value = count
        bucket.exact = True
    return buckets


def _semantics(
    spec: DimensionSpec, split_spec: DimensionSpec | None, multi_series: bool, series_disjoint: bool
) -> GroupbySemantics:
    if spec.multi_valued or (split_spec is not None and split_spec.multi_valued):
        return "overlapping"
    if multi_series and not series_disjoint:
        return "overlapping"
    return "partition"


def _config(
    *,
    sort: Literal["natural", "value_desc", "value_asc"],
    semantics: GroupbySemantics,
    rows: list[Row],
    measure_key: str,
    series: list[SeriesData],
    shown_buckets: Iterable[Bucket],
    unclassified_ids: set[str],
    value_format: Literal["integer", "number"] = "integer",
    y_zero_based: bool = True,
) -> ChartConfig:
    """Chart-level bookkeeping: what the values add up to and how many trials are behind them."""
    bucket_sum = sum(int(r.model_dump()[measure_key]) for r in rows)
    exact_series = [s for s in series if s.exact_counts]
    if exact_series:
        # Registry counts: the distinct total is only known when the dimension partitions a single series.
        if semantics == "partition" and len(series) == 1:
            total = series[0].total_matching
            distinct: int | None = bucket_sum
            unclassified: int | None = max(total - bucket_sum, 0) if total is not None else None
            hidden: int | None = 0
        else:
            distinct = unclassified = hidden = None
    else:
        shown_ids: set[str] = set()
        for b in shown_buckets:
            shown_ids |= b.trial_ids
        all_ids: set[str] = set()
        for s in series:
            all_ids |= s.trial_ids
        distinct = len(shown_ids)
        unclassified = len(unclassified_ids - shown_ids)
        hidden = len(all_ids - shown_ids - unclassified_ids)
    return ChartConfig(
        sort=sort,
        stacked=False,
        value_format=value_format,
        y_zero_based=y_zero_based,
        groupby_semantics=semantics,
        bucket_sum=bucket_sum,
        distinct_trials=distinct,
        unclassified_count=unclassified,
        hidden_count=hidden,
    )


# ---------------------------------------------------------------- bars


def _build_bars(plan: QueryPlan, series: list[SeriesData], limit: int, series_disjoint: bool) -> BuildResult:
    assert plan.dimension is not None
    spec = spec_for(plan.dimension)
    measure = plan.measure
    warnings: list[str] = []
    multi_series = plan.analysis == AnalysisType.COMPARISON
    split_spec = spec_for(plan.split_by) if plan.split_by else None
    with_code = spec.dimension not in (Dimension.COUNTRY, Dimension.SPONSOR, Dimension.CONDITION, Dimension.INTERVENTION)
    rows: list[Row] = []
    shown: list[Bucket] = []
    unclassified_ids: set[str] = set()
    categories_shown = 0
    categories_total = 0

    if split_spec is not None:
        # Primary dimension on x, split dimension as color. Comparisons with a split are rejected by
        # normalize, so exactly one series is present here.
        s = series[0]
        pairs = group_by_pair(s.trials, spec, split_spec, measure, granularity=plan.time_granularity)
        primary = _primary_order(pairs, spec, plan.top_n)
        split_labels = sorted({k[1] for k in pairs}, key=lambda lab: (split_spec.order_index(lab), lab))
        codes = {label: bucket.code for (label, _), bucket in pairs.items()}
        for p_label in primary:
            for s_label in split_labels:
                bucket = pairs.get((p_label, s_label))
                values = {spec.key: p_label}
                if with_code:
                    values[spec.code_key] = codes.get(p_label)
                values["series"] = s_label
                values[measure.value] = bucket.value if bucket else 0
                rows.append(_row(values, bucket, limit, s.exactness))
                if bucket is not None:
                    shown.append(bucket)
        paired_ids: set[str] = set()
        for b in pairs.values():
            paired_ids |= b.trial_ids
        unclassified_ids = s.trial_ids - paired_ids
        color = Channel(field="series", type="nominal", title=split_spec.title)
        categories_total, categories_shown = len({k[0] for k in pairs}), len(primary)
        warnings += _trim_warning(categories_total, categories_shown, spec)
        if unclassified_ids:
            warnings.append(_uncategorized_warning(len(unclassified_ids), spec, ""))
    else:
        category_order: list[str] = []
        codes: dict[str, str | None] = {}
        per_series: list[tuple[SeriesData, dict[str, Bucket]]] = []
        for s in series:
            result = group_by(s.trials, spec, measure, granularity=plan.time_granularity)
            buckets = _apply_exact(result.buckets, spec, s.exact_counts)
            ordered, before = order_buckets(buckets, spec, plan.top_n)
            unclassified_ids |= result.unclassified_ids
            categories_total = max(categories_total, before)
            warnings += _trim_warning(before, len(ordered), spec, s.label if multi_series else "")
            if result.uncategorized:
                warnings.append(_uncategorized_warning(result.uncategorized, spec, s.label if multi_series else ""))
            if result.skipped_no_measure:
                warnings.append(f"{result.skipped_no_measure} trials have no enrollment figure and were skipped.")
            for b in ordered:
                if b.label not in category_order:
                    category_order.append(b.label)
                if b.code is not None:
                    codes.setdefault(b.label, b.code)
            per_series.append((s, {b.label: b for b in ordered}))
        if multi_series and spec.natural_order is not None:
            category_order.sort(key=lambda lab: (spec.order_index(lab), lab))
        categories_shown = len(category_order)
        for s, by_label in per_series:
            for label in category_order:
                bucket = by_label.get(label)
                values = {spec.key: label}
                if with_code:
                    values[spec.code_key] = codes.get(label)
                if multi_series:
                    values["series"] = s.label
                values[measure.value] = bucket.value if bucket else 0
                exact_count = bucket.value if (bucket and bucket.exact and measure == Measure.TRIAL_COUNT) else None
                rows.append(_row(values, bucket, limit, _bucket_exactness(bucket, s), citation_count=exact_count))
                if bucket is not None:
                    shown.append(bucket)
        color = Channel(field="series", type="nominal", title="Series") if multi_series else None

    sort: Literal["natural", "value_desc"] = "natural" if spec.natural_order is not None else "value_desc"
    encoding = ChartEncoding(
        x=Channel(field=spec.key, type="nominal", title=spec.title),
        y=Channel(field=measure.value, type="quantitative", title=_measure_title(measure)),
        color=color,
        tooltip=[spec.key, measure.value, "citation_count"] + (["series"] if color else []),
    )
    columns = _dimension_columns(spec, measure, with_series=color is not None, with_code=with_code)
    notes = _sample_notes(series)
    extra_sub = (
        [f"top {categories_shown} of {categories_total} {spec.title.lower()} values"]
        if categories_total > categories_shown
        else []
    )
    config = _config(
        sort=sort,
        semantics=_semantics(spec, split_spec, multi_series, series_disjoint),
        rows=rows,
        measure_key=measure.value,
        series=series,
        shown_buckets=shown,
        unclassified_ids=unclassified_ids,
    )
    common = dict(title=title_for(plan), subtitle=subtitle_for(plan, extra_sub), encoding=encoding, columns=columns, data=rows)
    if color is not None:
        spec_out: VisualizationSpec = GroupedBarChartSpec(type="grouped_bar_chart", config=config, **common)
    else:
        spec_out = BarChartSpec(type="bar_chart", config=config, **common)
    return BuildResult(spec=spec_out, warnings=warnings, notes=notes)


def _primary_order(pairs: dict[tuple[str, str], Bucket], spec: DimensionSpec, top_n: int | None) -> list[str]:
    totals: dict[str, int] = {}
    for (p_label, _), bucket in pairs.items():
        totals[p_label] = totals.get(p_label, 0) + bucket.value
    if spec.dimension == Dimension.START_YEAR:
        return sorted(totals, key=lambda lab: lab)
    if spec.natural_order is not None:
        return sorted(totals, key=lambda lab: (spec.order_index(lab), lab))
    labels = sorted(totals, key=lambda lab: (-totals[lab], lab))
    limit = top_n if top_n is not None else spec.default_top_n
    return labels[:limit] if limit else labels


def _trim_warning(before: int, after: int, spec: DimensionSpec, series_label: str = "") -> list[str]:
    if before <= after:
        return []
    prefix = f"Series '{series_label}': " if series_label else ""
    return [f"{prefix}Showing the {after} largest of {before} {spec.title.lower()} values."]


def _uncategorized_warning(n: int, spec: DimensionSpec, series_label: str = "") -> str:
    prefix = f"Series '{series_label}': " if series_label else ""
    what = "no start date" if spec.dimension == Dimension.START_YEAR else f"no {spec.title.lower()} value"
    return f"{prefix}{n} fetched trials have {what} and are not shown."


# ---------------------------------------------------------------- time series


def _build_time_series(plan: QueryPlan, series: list[SeriesData], limit: int, series_disjoint: bool) -> BuildResult:
    spec = spec_for(Dimension.START_YEAR)
    measure = plan.measure
    gran = plan.time_granularity
    warnings: list[str] = []
    rows: list[Row] = []
    shown: list[Bucket] = []
    unclassified_ids: set[str] = set()
    multi_series = len(series) > 1
    split_spec = spec_for(plan.split_by) if plan.split_by else None
    lo, hi = plan.filter.start_year, plan.filter.end_year

    if split_spec is not None:
        s = series[0]
        pairs = group_by_pair(s.trials, spec, split_spec, measure, granularity=gran)
        split_labels = sorted({k[1] for k in pairs}, key=lambda lab: (split_spec.order_index(lab), lab))
        for s_label in split_labels:
            buckets = [b for (p, sl), b in pairs.items() if sl == s_label]
            buckets.sort(key=lambda b: b.period_start or b.label)
            for b in fill_period_gaps(buckets, lo, hi, gran):
                values = {"period": b.label, "period_start": b.period_start, "series": s_label, measure.value: b.value}
                rows.append(_row(values, b, limit, s.exactness))
                shown.append(b)
        paired_ids: set[str] = set()
        for b in pairs.values():
            paired_ids |= b.trial_ids
        unclassified_ids = s.trial_ids - paired_ids
        if unclassified_ids:
            warnings.append(_uncategorized_warning(len(unclassified_ids), spec, ""))
        color = Channel(field="series", type="nominal", title=split_spec.title)
    else:
        for s in series:
            result = group_by(s.trials, spec, measure, granularity=gran)
            buckets = _apply_exact(result.buckets, spec, s.exact_counts)
            ordered, _ = order_buckets(buckets, spec, None)
            unclassified_ids |= result.unclassified_ids
            if result.uncategorized:
                warnings.append(_uncategorized_warning(result.uncategorized, spec, s.label if multi_series else ""))
            for b in fill_period_gaps(ordered, lo, hi, gran):
                values = {"period": b.label, "period_start": b.period_start, measure.value: b.value}
                if multi_series:
                    values["series"] = s.label
                exact_count = b.value if (b.exact and measure == Measure.TRIAL_COUNT) else None
                rows.append(_row(values, b, limit, _bucket_exactness(b, s), citation_count=exact_count))
                shown.append(b)
        color = Channel(field="series", type="nominal", title="Series") if multi_series else None

    encoding = ChartEncoding(
        x=Channel(field="period_start", type="temporal", title=f"Start {gran.value}", granularity=gran),
        y=Channel(field=measure.value, type="quantitative", title=_measure_title(measure)),
        color=color,
        tooltip=["period", measure.value, "citation_count"] + (["series"] if color else []),
    )
    columns = [
        Column(
            name="period",
            type="string",
            description=f"Period label at {gran.value} granularity, e.g. '2019', '2019-Q2', or '2019-04'",
        ),
        Column(name="period_start", type="string", description="ISO date of the first day of the period, for temporal axes"),
        *([Column(name="series", type="string", description="Series label")] if color else []),
        Column(name=measure.value, type="integer", description=f"{_measure_title(measure)} started in the period"),
        *_citation_columns(),
    ]
    config = _config(
        sort="natural",
        semantics=_semantics(spec, split_spec, multi_series, series_disjoint),
        rows=rows,
        measure_key=measure.value,
        series=series,
        shown_buckets=shown,
        unclassified_ids=unclassified_ids,
    )
    spec_out = TimeSeriesSpec(
        type="time_series",
        title=title_for(plan),
        subtitle=subtitle_for(plan, []),
        encoding=encoding,
        columns=columns,
        config=config,
        data=rows,
    )
    return BuildResult(spec=spec_out, warnings=warnings, notes=_sample_notes(series))


# ---------------------------------------------------------------- network


def _build_network(plan: QueryPlan, series: list[SeriesData], limit: int) -> BuildResult:
    assert plan.network is not None
    s = series[0]
    result = build_network(s.trials, plan.network, max_edges=plan.top_n, citations_per_datum=limit, exactness=s.exactness)
    warnings: list[str] = []
    if result.edges_before_trim > len(result.edges):
        warnings.append(f"Showing the {len(result.edges)} heaviest of {result.edges_before_trim} edges.")
    if not result.edges:
        warnings.append("No linked pairs were found in the fetched trials.")
    if result.trials_without_arm_data:
        warnings.append(
            f"{result.trials_without_arm_data:,} fetched trials list no arm groups, so their drugs cannot be placed in "
            "the same arm and they do not contribute edges."
        )
    notes = _sample_notes(series) + [
        f"{result.trials_with_pairs:,} of {len(s.trials):,} fetched trials contained at least one linked pair.",
        f"Edge meaning: {result.edge_semantics}.",
    ]
    extra = [f"{len(result.nodes)} nodes, {len(result.edges)} edges, weight = trials in common"]
    spec_out = NetworkGraphSpec(
        type="network_graph",
        title=title_for(plan),
        subtitle=subtitle_for(plan, extra),
        node_kinds=result.node_kinds,
        edge_semantics=result.edge_semantics,
        trials_considered=result.trials_considered,
        trials_skipped=result.trials_without_arm_data,
        data=NetworkData(nodes=result.nodes, edges=result.edges),
    )
    return BuildResult(spec=spec_out, warnings=warnings, notes=notes)


# ---------------------------------------------------------------- scatter


def _build_scatter(plan: QueryPlan, series: list[SeriesData], limit: int) -> BuildResult:
    assert plan.numeric_x is not None and plan.numeric_y is not None
    s = series[0]
    group_spec = spec_for(plan.split_by) if plan.split_by else None

    def group_of(trial: Trial) -> str | None:
        if group_spec is None:
            return None
        cats = group_spec.extract(trial)
        return cats[0].label if cats else MISSING

    points, skipped = scatter_points(s.trials, plan.numeric_x, plan.numeric_y, group_of)
    rows: list[Row] = []
    for p in points:
        values = {
            "nct_id": p.trial.nct_id,
            "title": p.trial.brief_title,
            plan.numeric_x.value: _plain_number(p.x),
            plan.numeric_y.value: _plain_number(p.y),
        }
        if group_spec is not None:
            values["group"] = p.group
        citation = [
            Citation(
                nct_id=p.trial.nct_id,
                title=p.trial.brief_title,
                url=p.trial.url,
                evidence=[p.trial.evidence_title(), *p.evidence],
            )
        ]
        rows.append(Row(**values, exactness="exact", citations=citation if limit else [], citation_count=1))
    warnings = [f"{skipped} fetched trials lack one of the two values and are not plotted."] if skipped else []
    x_type: Literal["quantitative", "temporal"] = "quantitative"
    encoding = ChartEncoding(
        x=Channel(field=plan.numeric_x.value, type=x_type, title=NUMERIC_TITLES[plan.numeric_x]),
        y=Channel(field=plan.numeric_y.value, type="quantitative", title=NUMERIC_TITLES[plan.numeric_y]),
        color=Channel(field="group", type="nominal", title=group_spec.title) if group_spec else None,
        tooltip=["nct_id", "title", plan.numeric_x.value, plan.numeric_y.value] + (["group"] if group_spec else []),
    )
    columns = [
        Column(name="nct_id", type="string", description="Trial identifier; each point is one trial"),
        Column(name="title", type="string", description="Brief title"),
        Column(name=plan.numeric_x.value, type="number", description=NUMERIC_TITLES[plan.numeric_x]),
        Column(name=plan.numeric_y.value, type="number", description=NUMERIC_TITLES[plan.numeric_y]),
        *([Column(name="group", type="string", description=group_spec.title)] if group_spec else []),
        *_citation_columns(),
    ]
    config = ChartConfig(
        sort="natural",
        value_format="number",
        y_zero_based=False,
        groupby_semantics="partition",
        bucket_sum=None,
        distinct_trials=len(rows),
        unclassified_count=skipped,
        hidden_count=0,
    )
    spec_out = ScatterPlotSpec(
        type="scatter_plot",
        title=title_for(plan),
        subtitle=subtitle_for(plan, [f"{len(rows):,} trials plotted"]),
        encoding=encoding,
        columns=columns,
        config=config,
        data=rows,
    )
    return BuildResult(spec=spec_out, warnings=warnings, notes=_sample_notes(series))


# ---------------------------------------------------------------- histogram


def _build_histogram(plan: QueryPlan, series: list[SeriesData], limit: int) -> BuildResult:
    assert plan.numeric_x is not None
    s = series[0]
    result = histogram(s.trials, plan.numeric_x)
    rows: list[Row] = []
    for i, b in enumerate(result.buckets):
        lo = result.edges[i]
        hi = result.edges[i + 1] if i + 1 < len(result.edges) else None
        rows.append(_row({"bin": b.label, "bin_start": lo, "bin_end": hi, "trial_count": b.value}, b, limit, s.exactness))
    warnings = (
        [f"{result.missing} fetched trials have no {NUMERIC_TITLES[plan.numeric_x].lower()} and are not binned."]
        if result.missing
        else []
    )
    encoding = ChartEncoding(
        x=Channel(field="bin", type="ordinal", title=NUMERIC_TITLES[plan.numeric_x]),
        y=Channel(field="trial_count", type="quantitative", title="Number of trials"),
        tooltip=["bin", "trial_count", "citation_count"],
    )
    columns = [
        Column(name="bin", type="string", description="Bin label; rows are in ascending bin order"),
        Column(name="bin_start", type="number", description="Inclusive lower edge"),
        Column(name="bin_end", type="number", description="Exclusive upper edge, null for the open-ended last bin"),
        Column(name="trial_count", type="integer", description="Trials whose value falls in the bin"),
        *_citation_columns(),
    ]
    binned: set[str] = set()
    for b in result.buckets:
        binned |= b.trial_ids
    config = _config(
        sort="natural",
        semantics="partition",
        rows=rows,
        measure_key="trial_count",
        series=series,
        shown_buckets=result.buckets,
        unclassified_ids=s.trial_ids - binned,
    )
    spec_out = HistogramSpec(
        type="histogram",
        title=title_for(plan),
        subtitle=subtitle_for(plan, [f"{sum(b.value for b in result.buckets):,} trials binned"]),
        encoding=encoding,
        columns=columns,
        config=config,
        bins=BinSpec(field=plan.numeric_x.value, edges=result.edges, unit=_unit(plan)),
        data=rows,
    )
    return BuildResult(spec=spec_out, warnings=warnings, notes=_sample_notes(series))


def _plain_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _unit(plan: QueryPlan) -> str | None:
    return {"enrollment": "participants", "duration_months": "months", "site_count": "sites", "start_year": "year"}.get(
        plan.numeric_x.value if plan.numeric_x else "", None
    )
