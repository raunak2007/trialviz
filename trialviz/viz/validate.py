"""Independent verification of a finished visualization against the trial records.

The builders are trusted to be right, but a chart whose numbers do not match
its citations would be worse than no chart, so before a response is returned
every row, node, and edge is re-derived here with a separate pass over the
records. The checks share the dimension and entity definitions with the
analysis layer (what "phase" means is one definition), but none of the
bucketing, trimming, exact-count merging, or network accumulation code.

A failure raises `SpecInvalid`, which the API turns into a 500. The passing
report is attached to the response as `meta.verification` so a reader can see
what was checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import combinations, product

from trialviz.analysis.aggregate import categories_of
from trialviz.analysis.dimensions import MISSING, spec_for
from trialviz.analysis.drugs import DrugCanonicalizer
from trialviz.analysis.networks import ROLES, Entity, entities_for, same_arm_pairs
from trialviz.analysis.numeric import numeric_value
from trialviz.ctgov.records import (
    P_ARM_GROUPS,
    P_COLLABORATORS,
    P_COMPLETION,
    P_CONDITION_MESH,
    P_CONDITIONS,
    P_ENROLLMENT,
    P_INTERVENTION_MESH,
    P_INTERVENTIONS,
    P_LEAD_SPONSOR,
    P_LEAD_SPONSOR_CLASS,
    P_LOCATIONS,
    P_PHASES,
    P_START,
    P_STATUS,
    P_STUDY_TYPE,
    P_TITLE,
    Trial,
    encode_value,
)
from trialviz.schemas.enums import Dimension, Measure, NetworkKind
from trialviz.schemas.plan import QueryPlan
from trialviz.schemas.response import (
    Citation,
    NetworkGraphSpec,
    ScatterPlotSpec,
    Verification,
    VerificationCheck,
    VisualizationSpec,
)
from trialviz.viz.build import SeriesData

METHOD = (
    "Every row, node, and edge was re-derived from the fetched trial records by a second pass that shares only the "
    "field definitions with the aggregation code; every citation was checked for membership in its datum and every "
    "cited field value against the record. Registry counts cannot be re-derived locally and are checked for "
    "internal consistency instead."
)


class SpecInvalid(Exception):
    pass


def verify_spec(spec: VisualizationSpec, plan: QueryPlan, series: Sequence[SeriesData]) -> Verification:
    """Run every check; raise SpecInvalid on the first failure, return the report otherwise."""
    checks: list[VerificationCheck] = []
    by_id: dict[str, Trial] = {t.nct_id: t for s in series for t in s.trials}
    by_series: dict[str, dict[str, Trial]] = {s.label: {t.nct_id: t for t in s.trials} for s in series}

    checks.append(_check_structure(spec))
    checks.append(_check_citation_records(spec, by_id))
    if isinstance(spec, NetworkGraphSpec):
        assert plan.network is not None
        checks.append(_check_network(spec, plan.network, series[0].trials))
        checks.append(VerificationCheck(name="partition_sum", status="skipped", detail="not a chart with rows"))
    else:
        checks.append(_check_membership_and_values(spec, plan, series, by_series))
        checks.append(_check_partition_sum(spec, plan, series))
    return Verification(method=METHOD, checks=checks)


# ---------------------------------------------------------------- structure


def _check_structure(spec: VisualizationSpec) -> VerificationCheck:
    if isinstance(spec, NetworkGraphSpec):
        ids = {n.id for n in spec.data.nodes}
        if len(ids) != len(spec.data.nodes):
            raise SpecInvalid("duplicate node ids")
        for n in spec.data.nodes:
            if n.kind not in spec.node_kinds:
                raise SpecInvalid(f"node {n.id} has kind {n.kind!r} not listed in node_kinds")
        for e in spec.data.edges:
            if e.source not in ids or e.target not in ids:
                raise SpecInvalid(f"edge {e.source} -> {e.target} references an unknown node")
            if e.source == e.target:
                raise SpecInvalid(f"self-loop on {e.source}")
        return VerificationCheck(
            name="structure", status="passed", detail=f"{len(spec.data.nodes)} nodes and {len(spec.data.edges)} edges"
        )

    x_field, y_field = spec.encoding.x.field, spec.encoding.y.field
    declared = {c.name for c in spec.columns}
    if x_field not in declared or y_field not in declared:
        raise SpecInvalid(f"encoding fields {x_field!r}/{y_field!r} are not documented in columns")
    for i, row in enumerate(spec.data):
        values = row.model_dump()
        if x_field not in values or y_field not in values:
            raise SpecInvalid(f"row {i} lacks the encoded fields {x_field!r}/{y_field!r}")
        y = values[y_field]
        if not isinstance(y, (int, float)) or y < 0:
            raise SpecInvalid(f"row {i} has a non-numeric or negative value for {y_field!r}: {y!r}")
        if spec.encoding.color and spec.encoding.color.field not in values:
            raise SpecInvalid(f"row {i} lacks the color field {spec.encoding.color.field!r}")
        undeclared = set(values) - declared
        if undeclared:
            raise SpecInvalid(f"row {i} has undocumented keys {sorted(undeclared)}")
    return VerificationCheck(name="structure", status="passed", detail=f"{len(spec.data)} rows match the documented columns")


# ---------------------------------------------------------------- citations


def _evidence_universe(trial: Trial) -> set[tuple[str, str]]:
    """Every (field, value) pair a citation may legitimately quote from this record."""
    pairs: set[tuple[str, str]] = {
        (P_TITLE, trial.brief_title or ""),
        (P_PHASES, encode_value(list(trial.phases))),
        (P_STATUS, trial.overall_status or ""),
        (P_STUDY_TYPE, trial.study_type or ""),
        (P_LEAD_SPONSOR, trial.lead_sponsor or ""),
        (P_LEAD_SPONSOR_CLASS, trial.lead_sponsor_class or ""),
        (P_START, trial.start_date or ""),
        (P_COMPLETION, trial.completion_date or ""),
        (P_LOCATIONS, f"{trial.site_count} location entries"),
    }
    if trial.enrollment is not None:
        pairs.add((P_ENROLLMENT, encode_value(trial.enrollment)))
    for country, idx in trial.country_location_index.items():
        pairs.add((f"{P_LOCATIONS}[{idx}].country", country))
    for idx, c in enumerate(trial.conditions):
        pairs.add((f"{P_CONDITIONS}[{idx}]", c))
    for i in trial.interventions:
        pairs.add((f"{P_INTERVENTIONS}[{i.index}].name", i.name))
        pairs.add((f"{P_INTERVENTIONS}[{i.index}].type", i.type or ""))
    for idx, name in enumerate(trial.collaborators):
        pairs.add((f"{P_COLLABORATORS}[{idx}].name", name))
    for idx, term in enumerate(trial.condition_mesh_terms):
        pairs.add((f"{P_CONDITION_MESH}[{idx}].term", term))
    for idx, term in enumerate(trial.intervention_mesh_terms):
        pairs.add((f"{P_INTERVENTION_MESH}[{idx}].term", term))
    for arm_index, arm in enumerate(trial.arm_interventions):
        for name in arm:
            pairs.add((f"{P_ARM_GROUPS}[{arm_index}].interventionNames", name))
    return pairs


def _check_citation_records(spec: VisualizationSpec, by_id: dict[str, Trial]) -> VerificationCheck:
    cited = 0
    for where, citations, count in _all_citations(spec):
        if count < len(citations):
            raise SpecInvalid(f"{where}: citation_count {count} is smaller than the {len(citations)} citations attached")
        seen: set[str] = set()
        for c in citations:
            trial = by_id.get(c.nct_id)
            if trial is None:
                raise SpecInvalid(f"{where}: citation {c.nct_id} is not among the fetched trials")
            if c.nct_id in seen:
                raise SpecInvalid(f"{where}: citation {c.nct_id} appears twice")
            seen.add(c.nct_id)
            if not c.evidence:
                raise SpecInvalid(f"{where}: citation {c.nct_id} carries no evidence")
            if c.url != trial.url or (c.title or "") != (trial.brief_title or ""):
                raise SpecInvalid(f"{where}: citation {c.nct_id} has a title or url that does not match the record")
            universe = _evidence_universe(trial)
            for ev in c.evidence:
                if (ev.field, ev.value) not in universe:
                    raise SpecInvalid(f"{where}: citation {c.nct_id} quotes {ev.field}={ev.value!r}, which is not in the record")
            cited += 1
    return VerificationCheck(
        name="citations_match_records",
        status="passed",
        detail=f"{cited} citations point at fetched records and quote values present in them",
    )


def _all_citations(spec: VisualizationSpec) -> Iterable[tuple[str, list[Citation], int]]:
    if isinstance(spec, NetworkGraphSpec):
        for n in spec.data.nodes:
            yield f"node {n.id}", n.citations, n.citation_count
        for e in spec.data.edges:
            yield f"edge {e.source}->{e.target}", e.citations, e.citation_count
    else:
        for i, row in enumerate(spec.data):
            yield f"row {i}", row.citations, row.citation_count


# ---------------------------------------------------------------- rows: membership and recount


def _check_membership_and_values(
    spec: VisualizationSpec, plan: QueryPlan, series: Sequence[SeriesData], by_series: dict[str, dict[str, Trial]]
) -> VerificationCheck:
    assert not isinstance(spec, NetworkGraphSpec)
    if isinstance(spec, ScatterPlotSpec):
        return _check_scatter(spec, plan, series[0])
    if spec.type == "histogram":
        return _check_histogram(spec, plan, series[0])

    measure = plan.measure
    primary = spec_for(Dimension.START_YEAR if spec.type == "time_series" else plan.dimension)  # type: ignore[arg-type]
    split = spec_for(plan.split_by) if plan.split_by else None
    uses_canon = Dimension.INTERVENTION in (primary.dimension, split.dimension if split else None)
    canon = {s.label: DrugCanonicalizer(s.trials) if uses_canon else None for s in series}
    exact_by_series = {s.label: s.exact_counts or {} for s in series}
    y_field = spec.encoding.y.field
    recounted = 0
    exact_rows = 0
    for i, row in enumerate(spec.data):
        values = row.model_dump()
        label = values[primary.key]
        split_label = values.get("series") if split is not None else None
        series_label = values["series"] if split is None and len(series) > 1 else series[0].label
        trials = by_series[series_label]
        row_canon = canon[series_label]

        def in_row(trial: Trial, label=label, split_label=split_label, row_canon=row_canon) -> bool:
            labels = {c.label for c in categories_of(trial, primary, plan.time_granularity, row_canon)}
            if label not in labels:
                return False
            if split is None:
                return True
            return split_label in {c.label for c in categories_of(trial, split, plan.time_granularity, row_canon)}

        for c in row.citations:
            trial = trials.get(c.nct_id)
            if trial is None:
                raise SpecInvalid(f"row {i} ({label}): cited trial {c.nct_id} is not in series {series_label!r}")
            if not in_row(trial):
                raise SpecInvalid(f"row {i} ({label}): cited trial {c.nct_id} does not belong to this row")

        if label in exact_by_series[series_label]:
            # A registry count: it cannot be re-derived from a sample, but it must agree with itself.
            if values[y_field] != exact_by_series[series_label][label]:
                raise SpecInvalid(f"row {i} ({label}): value {values[y_field]} differs from the registry count")
            if measure == Measure.TRIAL_COUNT and row.citation_count != values[y_field]:
                raise SpecInvalid(f"row {i} ({label}): registry count {values[y_field]} differs from citation_count")
            if row.exactness != "exact":
                raise SpecInvalid(f"row {i} ({label}): registry count not marked exact")
            exact_rows += 1
            continue
        members = [t for t in trials.values() if in_row(t)]
        if measure == Measure.TOTAL_ENROLLMENT:
            expected = sum(t.enrollment for t in members if t.enrollment is not None)
            expected_count = sum(1 for t in members if t.enrollment is not None)
        else:
            expected = len(members)
            expected_count = len(members)
        if values[y_field] != expected:
            raise SpecInvalid(f"row {i} ({label}): value {values[y_field]} but {expected} re-derived from the records")
        if row.citation_count != expected_count:
            raise SpecInvalid(f"row {i} ({label}): citation_count {row.citation_count} but {expected_count} supporting trials")
        expected_exactness = "sampled" if next(s for s in series if s.label == series_label).truncated else "exact"
        if row.exactness != expected_exactness:
            raise SpecInvalid(f"row {i} ({label}): marked {row.exactness} but the series is {expected_exactness}")
        recounted += 1
    detail = f"{recounted} rows re-derived from {sum(len(s.trials) for s in series)} records"
    if exact_rows:
        detail += f"; {exact_rows} registry-count rows checked for internal consistency"
    return VerificationCheck(name="values_recounted", status="passed", detail=detail)


def _check_scatter(spec: ScatterPlotSpec, plan: QueryPlan, s: SeriesData) -> VerificationCheck:
    assert plan.numeric_x is not None and plan.numeric_y is not None
    by_id = {t.nct_id: t for t in s.trials}
    seen: set[str] = set()
    for i, row in enumerate(spec.data):
        values = row.model_dump()
        trial = by_id.get(values["nct_id"])
        if trial is None:
            raise SpecInvalid(f"row {i}: {values['nct_id']} is not a fetched trial")
        if trial.nct_id in seen:
            raise SpecInvalid(f"row {i}: {trial.nct_id} is plotted twice")
        seen.add(trial.nct_id)
        x, y = numeric_value(trial, plan.numeric_x), numeric_value(trial, plan.numeric_y)
        if x is None or y is None:
            raise SpecInvalid(f"row {i}: {trial.nct_id} lacks a plotted value")
        if float(values[plan.numeric_x.value]) != x[0] or float(values[plan.numeric_y.value]) != y[0]:
            raise SpecInvalid(f"row {i}: plotted values differ from the record for {trial.nct_id}")
        if plan.split_by is not None:
            cats = spec_for(plan.split_by).extract(trial)
            expected_group = cats[0].label if cats else MISSING
            if values.get("group") != expected_group:
                raise SpecInvalid(f"row {i}: group {values.get('group')!r} but record says {expected_group!r}")
        for c in row.citations:
            if c.nct_id != trial.nct_id:
                raise SpecInvalid(f"row {i}: cites {c.nct_id} but plots {trial.nct_id}")
    plottable = sum(1 for t in s.trials if numeric_value(t, plan.numeric_x) and numeric_value(t, plan.numeric_y))
    if plottable != len(spec.data):
        raise SpecInvalid(f"{len(spec.data)} points plotted but {plottable} fetched trials have both values")
    return VerificationCheck(
        name="values_recounted", status="passed", detail=f"{len(spec.data)} points re-read from their records"
    )


def _check_histogram(spec, plan: QueryPlan, s: SeriesData) -> VerificationCheck:
    assert plan.numeric_x is not None
    edges = spec.bins.edges
    by_id = {t.nct_id: t for t in s.trials}

    def bin_of(trial: Trial) -> int | None:
        found = numeric_value(trial, plan.numeric_x)
        if found is None or found[0] < edges[0]:
            return None
        for j in range(len(edges) - 1):
            if edges[j] <= found[0] < edges[j + 1]:
                return j
        return len(edges) - 1

    for i, row in enumerate(spec.data):
        values = row.model_dump()
        expected = sum(1 for t in s.trials if bin_of(t) == i)
        if values["trial_count"] != expected or row.citation_count != expected:
            raise SpecInvalid(f"bin {i} ({values['bin']}): value {values['trial_count']} but {expected} re-derived")
        for c in row.citations:
            if bin_of(by_id[c.nct_id]) != i:
                raise SpecInvalid(f"bin {i}: cited trial {c.nct_id} falls in another bin")
    return VerificationCheck(
        name="values_recounted", status="passed", detail=f"{len(spec.data)} bins re-derived from {len(s.trials)} records"
    )


# ---------------------------------------------------------------- partition sums


def _check_partition_sum(spec, plan: QueryPlan, series: Sequence[SeriesData]) -> VerificationCheck:
    cfg = spec.config
    if cfg.groupby_semantics != "partition":
        return VerificationCheck(
            name="partition_sum",
            status="skipped",
            detail="rows overlap (a trial can sit in several rows), so values are not summed",
        )
    if isinstance(spec, ScatterPlotSpec):
        fetched = {t.nct_id for s in series for t in s.trials}
        if cfg.distinct_trials + cfg.unclassified_count != len(fetched):
            raise SpecInvalid("scatter: plotted + skipped trials do not add up to the fetched records")
        return VerificationCheck(
            name="partition_sum",
            status="passed",
            detail=f"{cfg.distinct_trials} plotted + {cfg.unclassified_count} skipped = {len(fetched)} fetched",
        )
    y_field = spec.encoding.y.field
    total = sum(int(r.model_dump()[y_field]) for r in spec.data)
    if cfg.bucket_sum != total:
        raise SpecInvalid(f"bucket_sum {cfg.bucket_sum} but the rows sum to {total}")
    if any(s.exact_counts for s in series):
        if cfg.distinct_trials is not None and cfg.distinct_trials != total:
            raise SpecInvalid("registry counts under partition semantics must sum to distinct_trials")
        return VerificationCheck(name="partition_sum", status="passed", detail=f"registry counts sum to {total}")
    fetched = {t.nct_id for s in series for t in s.trials}
    if plan.measure == Measure.TRIAL_COUNT and cfg.distinct_trials != total:
        raise SpecInvalid(f"values sum to {total} but {cfg.distinct_trials} distinct trials are behind the rows")
    if cfg.distinct_trials + cfg.unclassified_count + cfg.hidden_count != len(fetched):
        raise SpecInvalid("distinct + unclassified + hidden trials do not add up to the fetched records")
    return VerificationCheck(
        name="partition_sum",
        status="passed",
        detail=f"{cfg.distinct_trials} in rows + {cfg.unclassified_count} unclassified + {cfg.hidden_count} hidden = {len(fetched)} fetched",
    )


# ---------------------------------------------------------------- network


def _check_network(spec: NetworkGraphSpec, kind: NetworkKind, trials: Sequence[Trial]) -> VerificationCheck:
    """Re-derive node and edge weights from the records with a plain loop over entity pairs."""
    canon = DrugCanonicalizer(trials)
    role_a, role_b = ROLES[kind]
    node_trials: dict[str, set[str]] = {}
    edge_trials: dict[tuple[str, str], set[str]] = {}
    for trial in trials:
        pairs = _pairs_for(trial, kind, role_a, role_b, canon)
        for a, b in pairs:
            edge_trials.setdefault((a.node_id, b.node_id), set()).add(trial.nct_id)
            node_trials.setdefault(a.node_id, set()).add(trial.nct_id)
            node_trials.setdefault(b.node_id, set()).add(trial.nct_id)
    for n in spec.data.nodes:
        expected = node_trials.get(n.id, set())
        if n.trial_count != len(expected) or n.citation_count != len(expected):
            raise SpecInvalid(f"node {n.id}: trial_count {n.trial_count} but {len(expected)} re-derived")
        for c in n.citations:
            if c.nct_id not in expected:
                raise SpecInvalid(f"node {n.id}: cited trial {c.nct_id} does not contain this entity in a linked pair")
    for e in spec.data.edges:
        expected = edge_trials.get((e.source, e.target), set()) | edge_trials.get((e.target, e.source), set())
        if e.trial_count != len(expected) or e.citation_count != len(expected):
            raise SpecInvalid(f"edge {e.source}->{e.target}: trial_count {e.trial_count} but {len(expected)} re-derived")
        for c in e.citations:
            if c.nct_id not in expected:
                raise SpecInvalid(f"edge {e.source}->{e.target}: cited trial {c.nct_id} does not contain this pair")
    return VerificationCheck(
        name="values_recounted",
        status="passed",
        detail=f"{len(spec.data.nodes)} node and {len(spec.data.edges)} edge weights re-derived from {len(trials)} records",
    )


def _pairs_for(
    trial: Trial, kind: NetworkKind, role_a: str, role_b: str, canon: DrugCanonicalizer
) -> list[tuple[Entity, Entity]]:
    if kind == NetworkKind.DRUG_DRUG:
        return same_arm_pairs(trial, canon) if trial.has_arm_data else []
    if role_a == role_b:
        return list(combinations(sorted(entities_for(trial, role_a, canon), key=lambda e: e.key), 2))
    return list(product(entities_for(trial, role_a, canon), entities_for(trial, role_b, canon)))
