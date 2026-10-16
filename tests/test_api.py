from fastapi.testclient import TestClient

from trialviz.app import create_app
from trialviz.config import Settings


def make_client(pipeline) -> TestClient:
    app = create_app(Settings(anthropic_api_key=None), pipeline=pipeline)
    return TestClient(app)


def test_query_endpoint_returns_visualization(pipeline):
    with make_client(pipeline) as c:
        r = c.post("/query", json={"query": "How are melanoma trials distributed across phases?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["visualization"]["type"] == "bar_chart"
        assert body["visualization"]["encoding"]["x"]["field"] == "phase"
        assert body["meta"]["source"]["name"] == "ClinicalTrials.gov"
        assert body["meta"]["planner"]["mode"] == "rules"
        first = body["visualization"]["data"][0]
        assert set(first) == {"phase", "phase_code", "trial_count", "citation_count", "citations"}


def test_structured_fields_and_options(pipeline):
    with make_client(pipeline) as c:
        r = c.post(
            "/query",
            json={
                "query": "How has the number of trials for this drug changed over time?",
                "drug_name": "Pembrolizumab",
                "citations_per_datum": 1,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["visualization"]["type"] == "time_series"
        assert body["meta"]["filters"]["drugs"] == ["Pembrolizumab"]
        assert all(len(row["citations"]) <= 1 for row in body["visualization"]["data"])


def test_error_envelopes(pipeline):
    with make_client(pipeline) as c:
        r = c.post("/query", json={"query": "Tell me about pembrolizumab"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "unsupported_query"
        assert r.json()["error"]["suggestions"]

        r = c.post("/query", json={"query": "Trials by phase"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "needs_clarification"

        r = c.post("/query", json={"query": "How are zzzz trials distributed across phases?"})
        assert r.status_code == 404 and r.json()["error"]["code"] == "no_data"

        r = c.post("/query", json={"query": "x", "max_trials": 5})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
        assert any("max_trials" in p for p in r.json()["error"]["details"]["problems"])

        r = c.post("/query", json={"query": "trials for melanoma by phase", "planner": "llm"})
        assert r.status_code == 422 and "ANTHROPIC_API_KEY" in r.json()["error"]["message"]


def test_health_schema_examples(pipeline):
    with make_client(pipeline) as c:
        assert c.get("/health").json() == {"status": "ok", "planner": "rules", "model": None}
        schema = c.get("/schema").json()
        assert "query" in schema["request"]["properties"]
        assert "visualization" in schema["response"]["properties"]
        assert c.get("/examples").status_code == 200
        assert c.get("/").status_code == 200
