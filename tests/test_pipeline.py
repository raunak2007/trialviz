"""End-to-end runs against the fake API through the real client, planner, aggregation, and builders."""

import pytest

from trialviz.pipeline import NoDataError
from trialviz.schemas.request import QueryRequest
from trialviz.schemas.response import QueryResponse
from trialviz.viz.validate import SpecInvalid, check_spec


def _values(resp: QueryResponse, key: str) -> dict:
    """{x value: key value} for every row of a chart response."""
    x_field = resp.visualization.encoding.x.field  # type: ignore[union-attr]
    return {r.model_dump()[x_field]: r.model_dump()[key] for r in resp.visualization.data}  # type: ignore[union-attr]


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
    assert resp.meta.retrieval[0].count_method == "sampled"


async def test_time_trend_sampled_when_everything_fits(pipeline):
    resp = await pipeline.run(
        QueryRequest(query="How has the number of trials for melanoma changed per year since 2015?", max_trials=50)
    )
    assert resp.visualization.type == "time_series"
    assert resp.meta.retrieval[0].count_method == "sampled"  # 9 matches fit in the sample, nothing to count exactly
    assert resp.meta.retrieval[0].truncated is False
    assert [r.model_dump()["period"] for r in resp.visualization.data] == [str(y) for y in range(2015, 2027)]


async def test_time_trend_exact_counts(pipeline, fake_api):
    resp = await pipeline.run(
        QueryRequest(query="How has the number of trials for melanoma changed per year since 2015?", count_mode="exact")
    )
    assert resp.meta.retrieval[0].count_method == "exact_server_counts"
    counts = _values(resp, "trial_count")
    assert counts["2015-01-01"] == 1 and counts["2021-01-01"] == 2 and counts["2017-01-01"] == 0
    assert len(counts) == 12
    # one count request per year, each carrying the base filter plus the year range
    year_requests = [r for r in fake_api.requests if r.get("pageSize") == "1"]
    assert len(year_requests) == 12
    assert all(r["query.cond"] == "melanoma" and r["filter.advanced"].startswith("AREA[StartDate]RANGE[") for r in year_requests)
    assert year_requests[0]["filter.advanced"] == "AREA[StartDate]RANGE[2015-01-01,2015-12-31]"


async def test_truncated_sample_triggers_exact_counts_in_auto_mode(pipeline):
    # max_trials is validated at 50 minimum; with 12 matching records the sample is complete, so force a
    # truncation by lowering the cap on the request object after validation.
    req = QueryRequest(query="How are melanoma trials distributed across phases?").model_copy(update={"max_trials": 3})
    resp = await pipeline.run(req)
    assert resp.meta.retrieval[0].truncated is True
    assert resp.meta.retrieval[0].count_method == "exact_server_counts"
    counts = _values(resp, "trial_count")
    assert sum(counts.values()) == resp.meta.retrieval[0].total_matching
    assert any("exact ClinicalTrials.gov counts" in a for a in resp.meta.assumptions)
    # citations still come from the 3 fetched records only
    cited = {c.nct_id for r in resp.visualization.data for c in r.citations}
    assert len(cited) <= 3


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


async def test_comparison(pipeline):
    resp = await pipeline.run(QueryRequest(query="Compare phases for trials involving pembrolizumab vs nivolumab"))
    viz = resp.visualization
    assert viz.type == "grouped_bar_chart" and viz.encoding.color.field == "series"
    rows = [r.model_dump() for r in viz.data]
    assert {r["series"] for r in rows} == {"pembrolizumab", "nivolumab"}
    assert len(resp.meta.retrieval) == 2
    phases = [r["phase"] for r in rows if r["series"] == "pembrolizumab"]
    assert phases == [r["phase"] for r in rows if r["series"] == "nivolumab"]  # same category order in both series


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


async def test_drug_cooccurrence_network(pipeline):
    resp = await pipeline.run(QueryRequest(query="Which drugs frequently co-occur in combination studies for melanoma?"))
    labels = {n.label for n in resp.visualization.data.nodes}
    assert {"Pembrolizumab", "Lenvatinib", "Nivolumab", "Ipilimumab"} <= labels
    assert "Placebo" not in labels


async def test_scatter_and_histogram(pipeline):
    resp = await pipeline.run(QueryRequest(query="Plot enrollment against start year for melanoma trials"))
    assert resp.visualization.type == "scatter_plot"
    assert all(r.citation_count == 1 for r in resp.visualization.data)
    resp = await pipeline.run(QueryRequest(query="Histogram of enrollment for melanoma trials"))
    assert resp.visualization.type == "histogram"
    assert resp.visualization.bins.edges[0] == 0


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
    assert list(first)[-2:] == ["citation_count", "citations"]  # chart fields come first


async def test_validator_catches_inconsistent_specs(pipeline):
    resp = await pipeline.run(QueryRequest(query="How are melanoma trials distributed across phases?"))
    fetched = {c.nct_id for r in resp.visualization.data for c in r.citations}
    check_spec(resp.visualization, fetched | {"NCT05000013", "NCT05000006"})
    with pytest.raises(SpecInvalid):
        check_spec(resp.visualization, {"NCT00000000"})
    with pytest.raises(SpecInvalid):
        check_spec(resp.visualization, fetched | {"NCT05000013", "NCT05000006"}, expected_sum=999)
