"""End-to-end runs against the fake API through the real client, planner, aggregation, builders, and verifier."""

import pytest

from trialviz.ctgov.records import parse_studies
from trialviz.pipeline import NoDataError
from trialviz.schemas.request import QueryRequest
from trialviz.schemas.response import QueryResponse
from trialviz.viz.build import SeriesData
from trialviz.viz.validate import SpecInvalid, verify_spec


def _values(resp: QueryResponse, key: str) -> dict:
    """{x value: key value} for every row of a chart response."""
    x_field = resp.visualization.encoding.x.field  # type: ignore[union-attr]
    return {r.model_dump()[x_field]: r.model_dump()[key] for r in resp.visualization.data}  # type: ignore[union-attr]


def _passed(resp: QueryResponse) -> dict[str, str]:
    return {c.name: c.status for c in resp.meta.verification.checks}


async def _series_for(client, resp: QueryResponse) -> list[SeriesData]:
    """Re-fetch the records behind a response so the verifier can be run on tampered copies."""
    out = []
    for r in resp.meta.retrieval:
        result = await client.fetch_studies(r.filter, max_records=2000)
        out.append(
            SeriesData(
                label=r.label,
                filter=r.filter,
                trials=parse_studies(result.studies),
                total_matching=result.total_count,
                truncated=result.truncated,
            )
        )
    return out


async def test_distribution_by_phase(pipeline):
    resp = await pipeline.run(QueryRequest(query="How are melanoma trials distributed across phases?"))
    viz = resp.visualization
    assert viz.type == "bar_chart"
    assert viz.encoding.x.field == "phase" and viz.encoding.y.field == "trial_count"
    counts = _values(resp, "trial_count")
    assert counts["Phase 3"] == 4 and counts["Phase 2"] == 4 and "Not specified" not in counts  # observational excluded
    assert sum(counts.values()) == resp.meta.retrieval[0].fetched
    row = next(r for r in viz.data if r.model_dump()["phase"] == "Phase 1/Phase 2")
    assert row.citations[0].nct_id == "NCT05000003"
    assert row.citations[0].evidence[1].value == '["PHASE1","PHASE2"]'
    assert resp.meta.plan.filter.study_types and resp.meta.planner.mode == "rules"
    assert resp.meta.api_calls and "query.cond=melanoma" in resp.meta.api_calls[0].url
    retrieval = resp.meta.retrieval[0]
    assert retrieval.count_method == "sampled" and retrieval.fetch_policy == "sample" and retrieval.coverage == 1.0
    # every record was fetched, so every value is exact and the bookkeeping adds up
    assert all(r.exactness == "exact" for r in viz.data)
    cfg = viz.config
    assert cfg.groupby_semantics == "partition"
    assert cfg.bucket_sum == cfg.distinct_trials == 11 and cfg.unclassified_count == 0 and cfg.hidden_count == 0
    assert _passed(resp) == {
        "structure": "passed",
        "citations_match_records": "passed",
        "values_recounted": "passed",
        "partition_sum": "passed",
    }


async def test_time_trend_sampled_when_everything_fits(pipeline):
    resp = await pipeline.run(
        QueryRequest(query="How has the number of trials for melanoma changed per year since 2015?", max_trials=50)
    )
    assert resp.visualization.type == "time_series"
    assert resp.meta.retrieval[0].count_method == "sampled"  # 9 matches fit in the sample, nothing to count exactly
    assert resp.meta.retrieval[0].truncated is False
    assert [r.model_dump()["period"] for r in resp.visualization.data] == [str(y) for y in range(2015, 2027)]
    assert resp.visualization.config.groupby_semantics == "partition"


async def test_time_trend_exact_counts(pipeline, fake_api):
    resp = await pipeline.run(
        QueryRequest(query="How has the number of trials for melanoma changed per year since 2015?", count_mode="exact")
    )
    assert resp.meta.retrieval[0].count_method == "exact_server_counts"
    counts = _values(resp, "trial_count")
    assert counts["2015-01-01"] == 1 and counts["2021-01-01"] == 2 and counts["2017-01-01"] == 0
    assert len(counts) == 12
    assert all(r.exactness == "exact" for r in resp.visualization.data)
    # one count request per year, each carrying the base filter plus the year range
    year_requests = [r for r in fake_api.requests if r.get("pageSize") == "1"]
    assert len(year_requests) == 12
    assert all(r["query.cond"] == "melanoma" and r["filter.advanced"].startswith("AREA[StartDate]RANGE[") for r in year_requests)
    assert year_requests[0]["filter.advanced"] == "AREA[StartDate]RANGE[2015-01-01,2015-12-31]"


async def test_truncated_sample_triggers_exact_counts_in_auto_mode(pipeline):
    # max_trials is validated at 50 minimum; with 11 matching records the sample is complete, so force a
    # truncation by lowering the cap on the request object after validation.
    req = QueryRequest(query="How are melanoma trials distributed across phases?").model_copy(update={"max_trials": 3})
    resp = await pipeline.run(req)
    retrieval = resp.meta.retrieval[0]
    assert retrieval.truncated is True and retrieval.coverage == round(3 / 11, 4)
    assert retrieval.count_method == "exact_server_counts"
    counts = _values(resp, "trial_count")
    assert sum(counts.values()) == retrieval.total_matching
    assert any("exact ClinicalTrials.gov counts" in a for a in resp.meta.assumptions)
    # registry counts are exact even though the sample is not, and the partition bookkeeping uses them
    assert all(r.exactness == "exact" for r in resp.visualization.data)
    assert resp.visualization.config.distinct_trials == 11
    # citations still come from the 3 fetched records only
    cited = {c.nct_id for r in resp.visualization.data for c in r.citations}
    assert len(cited) <= 3
    assert _passed(resp)["values_recounted"] == "passed"


async def test_truncated_sample_without_exact_counts_is_marked_sampled(pipeline):
    req = QueryRequest(query="How are melanoma trials distributed across phases?", count_mode="sample").model_copy(
        update={"max_trials": 3}
    )
    resp = await pipeline.run(req)
    assert resp.meta.retrieval[0].count_method == "sampled"
    assert all(r.exactness == "sampled" for r in resp.visualization.data)
    assert resp.visualization.config.distinct_trials == 3
    assert any("only the 3 most recently started" in a for a in resp.meta.assumptions)


async def test_phase_exact_counts_are_mutually_exclusive(pipeline, fake_api):
    resp = await pipeline.run(QueryRequest(query="How are melanoma trials distributed across phases?", count_mode="exact"))
    counts = _values(resp, "trial_count")
    assert counts == {"Phase 1": 1, "Phase 1/Phase 2": 1, "Phase 2": 4, "Phase 3": 4, "Not Applicable": 1}
    assert resp.meta.retrieval[0].count_method == "exact_server_counts"


async def test_country_exact_counts_use_sample_candidates(pipeline, fake_api):
    resp = await pipeline.run(QueryRequest(query="Which countries have the most trials for melanoma?", count_mode="exact"))
    counts = _values(resp, "trial_count")
    assert counts["United States"] == 8
    assert any('AREA[LocationCountry]"United States"' in r.get("filter.advanced", "") for r in fake_api.requests)
    cfg = resp.visualization.config
    assert cfg.groupby_semantics == "overlapping" and cfg.distinct_trials is None  # registry counts over an overlapping dimension
    assert _passed(resp)["partition_sum"] == "skipped"


async def test_geographic_counts_each_trial_once_per_country(pipeline):
    resp = await pipeline.run(QueryRequest(query="Which countries have the most trials for melanoma?"))
    cfg = resp.visualization.config
    assert resp.meta.retrieval[0].fetch_policy == "complete"
    assert cfg.groupby_semantics == "overlapping"
    assert cfg.bucket_sum > cfg.distinct_trials == 11  # multi-country trials sit in several rows
    assert cfg.unclassified_count == 1  # the withdrawn trial has no locations


async def test_comparison_discloses_overlap(pipeline):
    resp = await pipeline.run(QueryRequest(query="Compare phases for trials involving pembrolizumab vs nivolumab"))
    viz = resp.visualization
    assert viz.type == "grouped_bar_chart" and viz.encoding.color.field == "series"
    rows = [r.model_dump() for r in viz.data]
    assert {r["series"] for r in rows} == {"pembrolizumab", "nivolumab"}
    assert len(resp.meta.retrieval) == 2
    phases = [r["phase"] for r in rows if r["series"] == "pembrolizumab"]
    assert phases == [r["phase"] for r in rows if r["series"] == "nivolumab"]  # same category order in both series
    # NCT05000015 matches both searches and is counted in both series; the response says so as data and as text
    assert viz.config.groupby_semantics == "overlapping"
    assert [o.model_dump() for o in resp.meta.series_overlap] == [
        {"series": ["pembrolizumab", "nivolumab"], "trials_in_all": 1, "exact": True}
    ]
    assert any("counted in both series" in w for w in resp.meta.warnings)


async def test_comparison_partition_mode_counts_each_trial_once(pipeline):
    resp = await pipeline.run(
        QueryRequest(query="Compare phases for trials involving pembrolizumab vs nivolumab", comparison_mode="partition")
    )
    rows = [r.model_dump() for r in resp.visualization.data]
    assert {r["series"] for r in rows} == {"pembrolizumab only", "nivolumab only", "Both"}
    cfg = resp.visualization.config
    assert cfg.groupby_semantics == "partition"
    assert cfg.bucket_sum == cfg.distinct_trials == 6 + 4 - 1  # pembrolizumab (6) and nivolumab (4) share NCT05000015
    both = {r["phase"]: r["trial_count"] for r in rows if r["series"] == "Both"}
    assert both["Phase 2"] == 1 and sum(both.values()) == 1
    assert resp.meta.retrieval[0].fetch_policy == "complete"
    assert any("regrouped" in a for a in resp.meta.assumptions)
    assert _passed(resp)["partition_sum"] == "passed"


async def test_drug_match_reports_and_excludes_mention_only_trials(pipeline):
    req = QueryRequest(query="How are trials for this drug distributed across phases?", drug_name="pembrolizumab")
    resp = await pipeline.run(req)
    match = resp.meta.retrieval[0].drug_match
    assert match.model_dump() == {"mode": "any", "listed_as_intervention": 5, "mention_only": 1}
    assert sum(_values(resp, "trial_count").values()) == 6
    assert any("without listing it as an intervention" in a for a in resp.meta.assumptions)

    strict = await pipeline.run(req.model_copy(update={"drug_match": "strict"}))
    assert strict.meta.retrieval[0].drug_match.mode == "strict"
    assert strict.meta.retrieval[0].fetched == 6  # downloaded
    assert sum(_values(strict, "trial_count").values()) == 5  # charted
    assert strict.meta.retrieval[0].fetch_policy == "complete"
    assert "NCT05000015" not in {c.nct_id for r in strict.visualization.data for c in r.citations}
    assert any("excluded them" in a for a in strict.meta.assumptions)


async def test_network(pipeline):
    resp = await pipeline.run(
        QueryRequest(query="Show a network of sponsors and drugs for melanoma trials", citations_per_datum=1)
    )
    viz = resp.visualization
    assert viz.type == "network_graph" and viz.node_kinds == ["sponsor", "drug"]
    assert viz.encoding.nodes.size == "trial_count"
    ids = {n.id for n in viz.data.nodes}
    assert all(e.source in ids and e.target in ids for e in viz.data.edges)
    assert all(len(e.citations) == 1 for e in viz.data.edges)
    assert viz.trials_considered == 12 and viz.trials_skipped == 0 and "lead sponsor" in viz.edge_semantics
    assert all(n.exactness == "exact" for n in viz.data.nodes)
    assert resp.meta.retrieval[0].fetch_policy == "complete"
    assert _passed(resp)["values_recounted"] == "passed"


async def test_combination_network_uses_same_arm_not_same_trial(pipeline):
    resp = await pipeline.run(QueryRequest(query="Which drugs frequently co-occur in combination studies for melanoma?"))
    viz = resp.visualization
    assert resp.meta.plan.network == "drug_drug"
    labels = {n.label for n in viz.data.nodes}
    assert {"Pembrolizumab", "Lenvatinib", "Nivolumab", "Ipilimumab"} <= labels
    assert "Placebo" not in labels
    assert viz.trials_skipped == 2 and viz.trials_considered == 10  # the registry and the withdrawn trial list no arms
    assert any("list no arm groups" in w for w in resp.meta.warnings)

    # Pembrolizumab versus docetaxel: both drugs are in NCT05000009, in different arms.
    same_arm = await pipeline.run(
        QueryRequest(query="Show a network of drugs tested together in combination studies for non-small cell lung cancer")
    )
    assert same_arm.meta.plan.network == "drug_drug"
    pairs = {frozenset((e.source, e.target)) for e in same_arm.visualization.data.edges}
    assert frozenset(("drug:pembrolizumab", "drug:carboplatin")) in pairs
    assert frozenset(("drug:pembrolizumab", "drug:docetaxel")) not in pairs

    same_trial = await pipeline.run(QueryRequest(query="Which drugs co-occur in the same trials for non-small cell lung cancer?"))
    assert same_trial.meta.plan.network == "drug_drug_same_trial"
    pairs = {frozenset((e.source, e.target)) for e in same_trial.visualization.data.edges}
    assert frozenset(("drug:pembrolizumab", "drug:docetaxel")) in pairs
    assert "comparators" in same_trial.visualization.edge_semantics


async def test_complete_fetch_overrides_max_trials_up_to_the_ceiling(pipeline):
    req = QueryRequest(query="Show a network of sponsors and drugs for melanoma trials").model_copy(update={"max_trials": 3})
    resp = await pipeline.run(req)
    retrieval = resp.meta.retrieval[0]
    assert retrieval.fetch_policy == "complete" and retrieval.fetched == 12 and retrieval.coverage == 1.0
    assert all(n.exactness == "exact" for n in resp.visualization.data.nodes)

    above = await pipeline.run(req.model_copy(update={"complete_up_to": 5}))
    retrieval = above.meta.retrieval[0]
    assert retrieval.truncated and retrieval.fetched == 3
    assert any("complete_up_to ceiling" in w for w in above.meta.warnings)
    assert all(n.exactness == "sampled" for n in above.visualization.data.nodes)
    assert all(e.exactness == "sampled" for e in above.visualization.data.edges)


async def test_scatter_and_histogram(pipeline):
    resp = await pipeline.run(QueryRequest(query="Plot enrollment against start year for melanoma trials"))
    viz = resp.visualization
    assert viz.type == "scatter_plot"
    assert all(r.citation_count == 1 and r.exactness == "exact" for r in viz.data)
    assert viz.config.bucket_sum is None and viz.config.distinct_trials == len(viz.data) == 11
    assert viz.config.unclassified_count == 1  # no start date
    resp = await pipeline.run(QueryRequest(query="Histogram of enrollment for melanoma trials"))
    viz = resp.visualization
    assert viz.type == "histogram"
    assert viz.bins.edges[0] == 0
    assert viz.config.groupby_semantics == "partition" and viz.config.bucket_sum == viz.config.distinct_trials == 12
    assert _passed(resp)["partition_sum"] == "passed"


async def test_citations_can_be_disabled(pipeline):
    resp = await pipeline.run(QueryRequest(query="How are melanoma trials distributed across phases?", include_citations=False))
    assert all(r.citations == [] and r.citation_count > 0 for r in resp.visualization.data)


async def test_no_data(pipeline):
    with pytest.raises(NoDataError):
        await pipeline.run(QueryRequest(query="How are zzzz trials distributed across phases?"))


async def test_response_round_trips_through_schema(pipeline):
    resp = await pipeline.run(QueryRequest(query="How many trials started each year for melanoma?"))
    payload = resp.model_dump(mode="json")
    again = QueryResponse.model_validate(payload)
    assert again.visualization.type == "time_series"
    first = payload["visualization"]["data"][0]
    assert list(first)[-3:] == ["exactness", "citation_count", "citations"]  # chart fields come first
    assert payload["meta"]["verification"]["checks"]


# ---------------------------------------------------------------- tamper tests for the verifier


def _tampered(resp: QueryResponse, mutate) -> QueryResponse:
    payload = resp.model_dump(mode="json")
    mutate(payload["visualization"])
    return QueryResponse.model_validate(payload)


async def test_verifier_accepts_the_real_chart_and_rejects_tampered_copies(pipeline, client):
    resp = await pipeline.run(QueryRequest(query="How are melanoma trials distributed across phases?"))
    series = await _series_for(client, resp)
    plan = resp.meta.plan
    verify_spec(resp.visualization, plan, series)
    rows = resp.visualization.data
    phase3 = next(i for i, r in enumerate(rows) if r.model_dump()["phase"] == "Phase 3")
    phase2 = next(i for i, r in enumerate(rows) if r.model_dump()["phase"] == "Phase 2")

    def changed_count(viz):
        viz["data"][phase3]["trial_count"] += 1

    def changed_citation_count(viz):
        viz["data"][phase3]["citation_count"] -= 1

    def moved_citation(viz):
        viz["data"][phase2]["citations"].append(viz["data"][phase3]["citations"][0])
        viz["data"][phase2]["citation_count"] += 1

    def altered_evidence(viz):
        viz["data"][phase3]["citations"][0]["evidence"][1]["value"] = '["PHASE2"]'

    def altered_title(viz):
        viz["data"][phase3]["citations"][0]["title"] = "Something else"

    def wrong_sum(viz):
        viz["config"]["bucket_sum"] += 1

    def marked_sampled(viz):
        viz["data"][phase3]["exactness"] = "sampled"

    for mutate, message in [
        (changed_count, "re-derived"),
        (changed_citation_count, "supporting trials|smaller than"),
        (moved_citation, "does not belong"),
        (altered_evidence, "not in the record"),
        (altered_title, "does not match the record"),
        (wrong_sum, "rows sum to"),
        (marked_sampled, "marked sampled"),
    ]:
        with pytest.raises(SpecInvalid, match=message):
            verify_spec(_tampered(resp, mutate).visualization, plan, series)


async def test_verifier_rejects_tampered_networks(pipeline, client):
    resp = await pipeline.run(QueryRequest(query="Which drugs frequently co-occur in combination studies for melanoma?"))
    series = await _series_for(client, resp)
    verify_spec(resp.visualization, resp.meta.plan, series)

    def heavier_edge(viz):
        viz["data"]["edges"][0]["trial_count"] += 1
        viz["data"]["edges"][0]["citation_count"] += 1

    def wrong_citation(viz):
        other = next(c for e in viz["data"]["edges"][1:] for c in e["citations"] if c not in viz["data"]["edges"][0]["citations"])
        viz["data"]["edges"][0]["citations"].append(other)
        viz["data"]["edges"][0]["citation_count"] += 1

    for mutate, message in [(heavier_edge, "re-derived"), (wrong_citation, "re-derived|does not contain this pair")]:
        with pytest.raises(SpecInvalid, match=message):
            verify_spec(_tampered(resp, mutate).visualization, resp.meta.plan, series)


async def test_verifier_rejects_tampered_time_series_and_histograms(pipeline, client):
    resp = await pipeline.run(QueryRequest(query="How many trials started each year for melanoma?"))
    series = await _series_for(client, resp)
    busiest = max(range(len(resp.visualization.data)), key=lambda i: resp.visualization.data[i].model_dump()["trial_count"])

    def moved_trial(viz):
        viz["data"][busiest]["trial_count"] -= 1
        viz["data"][busiest]["citation_count"] -= 1
        viz["data"][busiest - 1]["trial_count"] += 1
        viz["data"][busiest - 1]["citation_count"] += 1

    with pytest.raises(SpecInvalid, match="re-derived|smaller than"):
        verify_spec(_tampered(resp, moved_trial).visualization, resp.meta.plan, series)

    resp = await pipeline.run(QueryRequest(query="Histogram of enrollment for melanoma trials"))
    series = await _series_for(client, resp)
    verify_spec(resp.visualization, resp.meta.plan, series)

    def wrong_bin(viz):
        viz["data"][0]["trial_count"] += 1
        viz["data"][0]["citation_count"] += 1

    with pytest.raises(SpecInvalid, match="re-derived"):
        verify_spec(_tampered(resp, wrong_bin).visualization, resp.meta.plan, series)
