from trialviz.ctgov.records import Trial, is_control_intervention, parse_partial_date, parse_studies, parse_study


def test_parse_study_reads_nested_fields(studies):
    trial = parse_study(studies[0])
    assert trial.nct_id == "NCT05000001"
    assert trial.phases == ("PHASE3",)
    assert trial.phase_label == "Phase 3"
    assert trial.start_year == 2021 and trial.start_month == 3
    assert trial.lead_sponsor == "Merck Sharp & Dohme LLC"
    assert trial.lead_sponsor_class == "INDUSTRY"
    assert trial.enrollment == 530
    assert trial.countries == ("United States", "France", "Germany", "Japan")  # de-duplicated, order kept
    assert trial.country_location_index["France"] == 2
    assert trial.site_count == 5
    assert [i.name for i in trial.interventions] == ["Pembrolizumab", "Lenvatinib"]
    assert trial.interventions[0].other_names == ("MK-3475", "KEYTRUDA")
    assert trial.intervention_mesh_terms == ("Pembrolizumab", "Lenvatinib")
    assert trial.url.endswith("/study/NCT05000001")


def test_phase_labels_follow_registry_convention():
    assert Trial(nct_id="x", phases=("PHASE1", "PHASE2")).phase_label == "Phase 1/Phase 2"
    assert Trial(nct_id="x", phases=("PHASE2", "PHASE3")).phase_label == "Phase 2/Phase 3"
    assert Trial(nct_id="x", phases=("NA",)).phase_label == "Not Applicable"
    assert Trial(nct_id="x", phases=()).phase_label == "Not specified"
    assert Trial(nct_id="x", phases=("EARLY_PHASE1",)).phase_label == "Early Phase 1"


def test_partial_dates():
    assert parse_partial_date("2019-03-15").isoformat() == "2019-03-15"
    assert parse_partial_date("2019-03").isoformat() == "2019-03-01"
    assert parse_partial_date("2019").isoformat() == "2019-01-01"
    assert parse_partial_date("") is None
    assert parse_partial_date("March 2019") is None
    assert parse_partial_date("2019-13") is None


def test_duration_and_missing_dates(studies):
    trials = {t.nct_id: t for t in parse_studies(studies)}
    assert trials["NCT05000001"].duration_months == 51.5
    assert trials["NCT05000013"].start_year is None
    assert trials["NCT05000013"].duration_months is None


def test_control_arms_are_excluded_from_drug_lists(studies):
    trials = {t.nct_id: t for t in parse_studies(studies)}
    assert [i.name for i in trials["NCT05000002"].drug_interventions()] == ["MK-3475"]
    assert is_control_intervention("Placebo")
    assert is_control_intervention("Matching placebo tablet")
    assert is_control_intervention("Standard of care")
    assert not is_control_intervention("Pembrolizumab")


def test_parse_studies_skips_bad_records_and_duplicates(studies):
    broken = {"protocolSection": {"identificationModule": {}}}
    trials = parse_studies([studies[0], broken, studies[0]])
    assert [t.nct_id for t in trials] == ["NCT05000001"]
