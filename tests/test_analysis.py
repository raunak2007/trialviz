from trialviz.analysis.aggregate import fill_period_gaps, group_by, group_by_pair, order_buckets
from trialviz.analysis.dimensions import spec_for
from trialviz.analysis.drugs import DrugCanonicalizer, clean_name, normalize_name
from trialviz.analysis.networks import build_network
from trialviz.analysis.numeric import histogram, scatter_points
from trialviz.ctgov.records import parse_studies
from trialviz.schemas.enums import Dimension, Measure, NetworkKind, NumericField, TimeGranularity


def trials_of(studies):
    return parse_studies(studies)


def test_phase_buckets_add_up_and_keep_natural_order(studies):
    trials = trials_of(studies)
    result = group_by(trials, spec_for(Dimension.PHASE), Measure.TRIAL_COUNT)
    ordered, before = order_buckets(result.buckets, spec_for(Dimension.PHASE), None)
    assert [b.label for b in ordered] == ["Phase 1", "Phase 1/Phase 2", "Phase 2", "Phase 3", "Not Applicable", "Not specified"]
    assert sum(b.value for b in ordered) == len(trials)
    assert before == 6
    # every bucket carries the exact phases array as evidence
    phase3 = next(b for b in ordered if b.label == "Phase 3")
    assert all(ev[0].field.endswith("designModule.phases") for _, ev in phase3.supporting)
    assert {ev[0].value for _, ev in phase3.supporting} == {'["PHASE3"]'}


def test_multi_valued_country_counts_each_trial_once_per_country(studies):
    trials = trials_of(studies)
    result = group_by(trials, spec_for(Dimension.COUNTRY), Measure.TRIAL_COUNT)
    by_label = {b.label: b for b in result.buckets}
    assert by_label["United States"].value == 10  # NCT05000001 lists the US twice but counts once
    assert result.uncategorized == 1  # the withdrawn trial has no locations
    ordered, _ = order_buckets(result.buckets, spec_for(Dimension.COUNTRY), 3)
    assert [b.label for b in ordered] == ["United States", "France", "Germany"]


def test_total_enrollment_measure_skips_missing(studies):
    trials = trials_of(studies)
    for t in trials:
        if t.nct_id == "NCT05000001":
            t.enrollment = None
    result = group_by(trials, spec_for(Dimension.SPONSOR_CLASS), Measure.TOTAL_ENROLLMENT)
    assert result.skipped_no_measure == 1
    industry = next(b for b in result.buckets if b.label == "Industry")
    assert industry.value == 120 + 85 + 870 + 40 + 1000 + 600 + 700 + 0


def test_time_periods_and_gap_filling(studies):
    trials = trials_of(studies)
    spec = spec_for(Dimension.START_YEAR)
    result = group_by(trials, spec, Measure.TRIAL_COUNT, granularity=TimeGranularity.YEAR)
    ordered, _ = order_buckets(result.buckets, spec, None)
    filled = fill_period_gaps(ordered, 2013, 2024, TimeGranularity.YEAR)
    assert [b.label for b in filled] == [str(y) for y in range(2013, 2025)]
    assert next(b for b in filled if b.label == "2014").value == 0
    assert next(b for b in filled if b.label == "2021").value == 2
    quarterly = group_by(trials, spec, Measure.TRIAL_COUNT, granularity=TimeGranularity.QUARTER)
    assert {b.label for b in quarterly.buckets if b.label.startswith("2021")} == {"2021-Q1", "2021-Q3"}


def test_gap_filling_never_drops_data_outside_the_window(studies):
    trials = trials_of(studies)
    spec = spec_for(Dimension.START_YEAR)
    ordered, _ = order_buckets(group_by(trials, spec, Measure.TRIAL_COUNT).buckets, spec, None)
    filled = fill_period_gaps(ordered, 2020, 2022, TimeGranularity.YEAR)
    assert filled[0].label == "2013" and filled[-1].label == "2024"


def test_pair_grouping(studies):
    trials = trials_of(studies)
    pairs = group_by_pair(trials, spec_for(Dimension.PHASE), spec_for(Dimension.SPONSOR_CLASS), Measure.TRIAL_COUNT)
    assert pairs[("Phase 3", "Industry")].value == 5
    assert pairs[("Phase 3", "NIH")].value == 1
    assert len(pairs[("Phase 3", "NIH")].supporting[0][1]) == 2  # evidence for both dimensions


def test_drug_name_folding(studies):
    trials = trials_of(studies)
    canon = DrugCanonicalizer(trials)
    assert canon.canonical("MK-3475") == "Pembrolizumab"
    assert canon.canonical("Keytruda") == "Pembrolizumab"
    assert canon.canonical("Pembrolizumab 200 mg IV") == "Pembrolizumab"
    assert canon.canonical("Dabrafenib 150 mg") == "Dabrafenib"
    assert canon.canonical("Relatlimab (BMS-986016)") == "Relatlimab"
    assert normalize_name("Nivolumab 3 mg/kg IV") == "nivolumab"
    assert clean_name("Aspirin (ASA)") == "Aspirin"


def test_intervention_dimension_uses_folded_names(studies):
    trials = trials_of(studies)
    result = group_by(trials, spec_for(Dimension.INTERVENTION), Measure.TRIAL_COUNT)
    by_label = {b.label: b.value for b in result.buckets}
    assert by_label["Pembrolizumab"] == 6
    assert "MK-3475" not in by_label and "Keytruda" not in by_label


def test_drug_drug_network_links_same_arm_only_and_skips_placebo(studies):
    trials = trials_of(studies)
    net = build_network(trials, NetworkKind.DRUG_DRUG, citations_per_datum=2)
    edges = {(e.source, e.target): e for e in net.edges}
    assert ("drug:dabrafenib", "drug:trametinib") in edges
    assert ("drug:ipilimumab", "drug:nivolumab") in edges
    assert ("drug:docetaxel", "drug:pembrolizumab") not in edges  # NCT05000009 compares them in separate arms
    assert not any("placebo" in k[0] or "placebo" in k[1] for k in edges)
    assert net.node_kinds == ["drug"]
    pembro = next(n for n in net.nodes if n.id == "drug:pembrolizumab")
    assert pembro.trial_count == 2  # with lenvatinib and with carboplatin/pemetrexed; not with docetaxel, placebo, or radiation
    edge = edges[("drug:dabrafenib", "drug:trametinib")]
    assert edge.citations[0].nct_id == "NCT05000005"
    assert any(ev.field.endswith("interventionNames") for ev in edge.citations[0].evidence)
    assert net.trials_without_arm_data == 2 and net.trials_considered == len(trials) - 2
    assert "same arm" in net.edge_semantics


def test_drug_drug_same_trial_network_includes_comparators(studies):
    trials = trials_of(studies)
    net = build_network(trials, NetworkKind.DRUG_DRUG_SAME_TRIAL)
    edges = {(e.source, e.target) for e in net.edges}
    assert ("drug:docetaxel", "drug:pembrolizumab") in edges
    pembro = next(n for n in net.nodes if n.id == "drug:pembrolizumab")
    assert pembro.trial_count == 3
    assert net.trials_without_arm_data == 0 and net.trials_considered == len(trials)
    assert all(n.exactness == "exact" for n in net.nodes)


def test_sponsor_drug_network_prunes_to_heaviest_edges(studies):
    trials = trials_of(studies)
    net = build_network(trials, NetworkKind.SPONSOR_DRUG, max_edges=3)
    assert len(net.edges) == 3
    assert net.edges[0].source == "sponsor:merck sharp & dohme llc" and net.edges[0].target == "drug:pembrolizumab"
    assert net.edges[0].trial_count == 4
    assert net.edges_before_trim > 3
    node_ids = {n.id for n in net.nodes}
    assert all(e.source in node_ids and e.target in node_ids for e in net.edges)
    assert net.node_kinds == ["sponsor", "drug"]


def test_condition_network_prefers_mesh_terms(studies):
    trials = trials_of(studies)
    net = build_network(trials, NetworkKind.CONDITION_DRUG)
    labels = {n.label for n in net.nodes if n.kind == "condition"}
    assert "Melanoma" in labels and "Metastatic Melanoma" not in labels


def test_histogram_bins(studies):
    trials = trials_of(studies)
    result = histogram(trials, NumericField.ENROLLMENT)
    counts = {b.label: b.value for b in result.buckets}
    assert counts["500 to 999"] == 5
    assert counts["1000 to 1999"] == 2
    assert sum(counts.values()) == len(trials)
    assert result.buckets[-1].label == "10000+"


def test_scatter_points_and_skips(studies):
    trials = trials_of(studies)
    points, skipped = scatter_points(trials, NumericField.START_YEAR, NumericField.ENROLLMENT, lambda t: t.phase_label)
    assert skipped == 1
    assert {p.group for p in points} >= {"Phase 3", "Phase 2"}
    assert all(len(p.evidence) == 2 for p in points)
