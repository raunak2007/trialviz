"""Assemble a VisualizationSpec from a plan and the trials retrieved per series.

Each analysis type has one builder. Builders only reshape aggregation output
into rows, encodings, and column docs; they never compute new numbers beyond
what `trialviz.analysis` produced.
"""

from __future__ import annotations

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


@dataclass
class BuildResult:
    spec: VisualizationSpec
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Independent expectation for sum(values), set when every fetched trial lands in exactly one
    # untrimmed, sampled bucket. `viz.validate` checks the chart against it.
    expected_sum: int | None = None


def build_visualization(
    plan: QueryPlan, series: list[SeriesData], *, include_citations: bool, citations_per_datum: int
) -> BuildResult:
    limit = citations_per_datum if include_citations else 0
    a = plan.analysis
    if a in (AnalysisType.DISTRIBUTION, AnalysisType.COMPARISON, AnalysisType.GEOGRAPHIC):
        if plan.dimension == Dimension.START_YEAR:
            return _build_time_series(plan, series, limit)
        return _build_bars(plan, series, limit)
    if a == AnalysisType.TIME_TREND:
        return _build_time_series(plan, series, limit)
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
        if not s.truncated or s.total_matching is None:
            continue
        prefix = f"Series '{s.label}': " if len(series) > 1 else ""
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


def _row(values: dict, bucket: Bucket | None, limit: int, citation_count: int | None = None) -> Row:
    citations: list[Citation] = bucket.citations(limit) if bucket is not None and limit else []
    count = citation_count if citation_count is not None else (bucket.trial_count if bucket else 0)
    return Row(**values, citations=citations, citation_count=count)


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
            if count == 0:
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


# ---------------------------------------------------------------- bars


def _build_bars(plan: QueryPlan, series: list[SeriesData], limit: int) -> BuildResult:
    assert plan.dimension is not None
    spec = spec_for(plan.dimension)
    measure = plan.measure
    warnings: list[str] = []
    multi_series = plan.analysis == AnalysisType.COMPARISON
    split_spec = spec_for(plan.split_by) if plan.split_by else None
    with_code = spec.dimension not in (Dimension.COUNTRY, Dimension.SPONSOR, Dimension.CONDITION, Dimension.INTERVENTION)
    rows: list[Row] = []
    categories_shown = 0
    categories_total = 0
    expected_sum: int | None = None

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
                rows.append(_row(values, bucket, limit))
        color = Channel(field="series", type="nominal", title=split_spec.title)
        categories_total, categories_shown = len({k[0] for k in pairs}), len(primary)
        warnings += _trim_warning(categories_total, categories_shown, spec)
    else:
        category_order: list[str] = []
        codes: dict[str, str | None] = {}
        per_series: list[tuple[SeriesData, dict[str, Bucket]]] = []
        for s in series:
            result = group_by(s.trials, spec, measure, granularity=plan.time_granularity)
            buckets = _apply_exact(result.buckets, spec, s.exact_counts)
            ordered, before = order_buckets(buckets, spec, plan.top_n)
            if (
                len(series) == 1
                and not spec.multi_valued
                and not s.exact_counts
                and before == len(ordered)
                and measure == Measure.TRIAL_COUNT
            ):
                expected_sum = len(s.trials) - result.uncategorized
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
                rows.append(_row(values, bucket, limit, citation_count=exact_count))
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
    common = dict(title=title_for(plan), subtitle=subtitle_for(plan, extra_sub), encoding=encoding, columns=columns, data=rows)
    config = ChartConfig(sort=sort, stacked=False, value_format="integer")
    if color is not None:
        spec_out: VisualizationSpec = GroupedBarChartSpec(type="grouped_bar_chart", config=config, **common)
    else:
        spec_out = BarChartSpec(type="bar_chart", config=config, **common)
    return BuildResult(spec=spec_out, warnings=warnings, notes=notes, expected_sum=expected_sum)


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


def _build_time_series(plan: QueryPlan, series: list[SeriesData], limit: int) -> BuildResult:
    spec = spec_for(Dimension.START_YEAR)
    measure = plan.measure
    gran = plan.time_granularity
    warnings: list[str] = []
    rows: list[Row] = []
    multi_series = len(series) > 1
    split_spec = spec_for(plan.split_by) if plan.split_by else None
    lo, hi = plan.filter.start_year, plan.filter.end_year
    expected_sum: int | None = None

    if split_spec is not None:
        s = series[0]
        pairs = group_by_pair(s.trials, spec, split_spec, measure, granularity=gran)
        split_labels = sorted({k[1] for k in pairs}, key=lambda lab: (split_spec.order_index(lab), lab))
        for s_label in split_labels:
            buckets = [b for (p, sl), b in pairs.items() if sl == s_label]
            buckets.sort(key=lambda b: b.period_start or b.label)
            for b in fill_period_gaps(buckets, lo, hi, gran):
                rows.append(
                    _row({"period": b.label, "period_start": b.period_start, "series": s_label, measure.value: b.value}, b, limit)
                )
        color = Channel(field="series", type="nominal", title=split_spec.title)
    else:
        for s in series:
            result = group_by(s.trials, spec, measure, granularity=gran)
            buckets = _apply_exact(result.buckets, spec, s.exact_counts)
            ordered, _ = order_buckets(buckets, spec, None)
            if result.uncategorized:
                warnings.append(_uncategorized_warning(result.uncategorized, spec, s.label if multi_series else ""))
            if not multi_series and not s.exact_counts and measure == Measure.TRIAL_COUNT:
                expected_sum = len(s.trials) - result.uncategorized
            for b in fill_period_gaps(ordered, lo, hi, gran):
                values = {"period": b.label, "period_start": b.period_start, measure.value: b.value}
                if multi_series:
                    values["series"] = s.label
                exact_count = b.value if (b.exact and measure == Measure.TRIAL_COUNT) else None
                rows.append(_row(values, b, limit, citation_count=exact_count))
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
    spec_out = TimeSeriesSpec(
        type="time_series",
        title=title_for(plan),
        subtitle=subtitle_for(plan, []),
        encoding=encoding,
        columns=columns,
        config=ChartConfig(sort="natural"),
        data=rows,
    )
    return BuildResult(spec=spec_out, warnings=warnings, notes=_sample_notes(series), expected_sum=expected_sum)


# ---------------------------------------------------------------- network


def _build_network(plan: QueryPlan, series: list[SeriesData], limit: int) -> BuildResult:
    assert plan.network is not None
    s = series[0]
    result = build_network(s.trials, plan.network, max_edges=plan.top_n, citations_per_datum=limit)
    warnings: list[str] = []
    if result.edges_before_trim > len(result.edges):
        warnings.append(f"Showing the {len(result.edges)} heaviest of {result.edges_before_trim} edges.")
    if not result.edges:
        warnings.append("No co-occurring pairs were found in the fetched trials.")
    notes = _sample_notes(series) + [
        f"{result.trials_with_pairs:,} of {len(s.trials):,} fetched trials contained at least one linked pair."
    ]
    extra = [f"{len(result.nodes)} nodes, {len(result.edges)} edges, weight = trials in common"]
    spec_out = NetworkGraphSpec(
        type="network_graph",
        title=title_for(plan),
        subtitle=subtitle_for(plan, extra),
        node_kinds=result.node_kinds,
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
        rows.append(Row(**values, citations=citation if limit else [], citation_count=1))
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
    spec_out = ScatterPlotSpec(
        type="scatter_plot",
        title=title_for(plan),
        subtitle=subtitle_for(plan, [f"{len(rows):,} trials plotted"]),
        encoding=encoding,
        columns=columns,
        config=ChartConfig(sort="natural", value_format="number", y_zero_based=False),
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
        rows.append(_row({"bin": b.label, "bin_start": lo, "bin_end": hi, "trial_count": b.value}, b, limit))
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
    spec_out = HistogramSpec(
        type="histogram",
        title=title_for(plan),
        subtitle=subtitle_for(plan, [f"{sum(b.value for b in result.buckets):,} trials binned"]),
        encoding=encoding,
        columns=columns,
        config=ChartConfig(sort="natural"),
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
