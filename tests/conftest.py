"""Shared fixtures: a fake ClinicalTrials.gov served from recorded study records.

The fake honours the subset of the real API that the client uses: query.cond /
query.intr / query.term / query.spons text matching, filter.overallStatus,
the Essie expressions the code emits in filter.advanced, pageSize + pageToken
pagination, countTotal, and StartDate sorting. Tests therefore exercise the
real client code, parameter builder, and pipeline without the network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from trialviz.ctgov.client import CTGovClient
from trialviz.interpret.planner import Planner
from trialviz.pipeline import Pipeline

FIXTURE = Path(__file__).parent / "fixtures" / "studies.json"


def load_studies() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["studies"]


def _text(study: dict[str, Any]) -> str:
    return json.dumps(study).casefold()


def _module(study: dict[str, Any], name: str) -> dict[str, Any]:
    return study.get("protocolSection", {}).get(name, {}) or {}


def _matches_term(study: dict[str, Any], expr: str, *fields: str) -> bool:
    """OR-separated terms; a term matches if it occurs in any of the given JSON paths (or anywhere when none)."""
    haystack = _text(study) if not fields else " ".join(json.dumps(_module(study, f)) for f in fields).casefold()
    terms = [t.strip(" ()").casefold() for t in re.split(r"\s+OR\s+", expr)]
    return any(t in haystack for t in terms)


def _phase_set(study: dict[str, Any]) -> set[str]:
    return set(_module(study, "designModule").get("phases", []) or [])


def _start_date(study: dict[str, Any]) -> str:
    return (_module(study, "statusModule").get("startDateStruct") or {}).get("date", "") or ""


def _eval_area(study: dict[str, Any], clause: str) -> bool:
    """Evaluate one AREA[...] clause the client can emit."""
    m = re.fullmatch(r"AREA\[(\w+)\](.+)", clause.strip())
    assert m, f"fake API cannot parse clause {clause!r}"
    field, value = m.group(1), m.group(2).strip('"')
    if field == "StartDate":
        r = re.fullmatch(r"RANGE\[([^,]+),([^\]]+)\]", value)
        assert r, value
        lo, hi = r.group(1), r.group(2)
        start = _start_date(study)
        if not start:
            return False
        padded = {4: start + "-01-01", 7: start + "-01"}.get(len(start), start)  # YYYY or YYYY-MM -> full date
        return (lo == "MIN" or padded >= lo) and (hi == "MAX" or padded <= hi)
    if field == "Phase":
        return value in _phase_set(study)
    if field == "StudyType":
        return _module(study, "designModule").get("studyType") == value
    if field == "OverallStatus":
        return _module(study, "statusModule").get("overallStatus") == value
    if field == "LeadSponsorClass":
        return (_module(study, "sponsorCollaboratorsModule").get("leadSponsor") or {}).get("class") == value
    if field == "InterventionType":
        return any(i.get("type") == value for i in _module(study, "armsInterventionsModule").get("interventions", []) or [])
    if field == "LocationCountry":
        return any(loc.get("country") == value for loc in _module(study, "contactsLocationsModule").get("locations", []) or [])
    raise AssertionError(f"fake API does not support AREA[{field}]")


def _eval_advanced(study: dict[str, Any], expr: str) -> bool:
    """Evaluate an Essie expression made of AND / OR / NOT and AREA clauses with optional parentheses."""
    tokens = re.findall(r"\(|\)|\bAND\b|\bOR\b|\bNOT\b|AREA\[\w+\](?:RANGE\[[^\]]+\]|\"[^\"]+\"|\S+)", expr)
    pos = 0

    def parse_or() -> bool:
        nonlocal pos
        left = parse_and()
        while pos < len(tokens) and tokens[pos] == "OR":
            pos += 1
            right = parse_and()
            left = left or right
        return left

    def parse_and() -> bool:
        nonlocal pos
        left = parse_not()
        while pos < len(tokens) and tokens[pos] == "AND":
            pos += 1
            right = parse_not()
            left = left and right
        return left

    def parse_not() -> bool:
        nonlocal pos
        if tokens[pos] == "NOT":
            pos += 1
            return not parse_not()
        return parse_atom()

    def parse_atom() -> bool:
        nonlocal pos
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            value = parse_or()
            assert tokens[pos] == ")"
            pos += 1
            return value
        pos += 1
        return _eval_area(study, tok)

    result = parse_or()
    assert pos == len(tokens), f"unparsed tokens in {expr!r}"
    return result


class FakeCTGov:
    """Serves /studies from a list of records. Records every request for assertions."""

    def __init__(self, studies: list[dict[str, Any]]):
        self.studies = studies
        self.requests: list[dict[str, str]] = []
        self.fail_statuses: list[int] = []  # status codes to return before succeeding, in order

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(str(request.url.query.decode()), keep_blank_values=True).items()}
        self.requests.append(params)
        if self.fail_statuses:
            status = self.fail_statuses.pop(0)
            return httpx.Response(status, json={"message": "simulated failure"})
        if "/studies" not in request.url.path:
            return httpx.Response(404, json={"message": "not found"})
        matched = [s for s in self.studies if self._matches(s, params)]
        matched.sort(key=_start_date, reverse=True)
        page_size = int(params.get("pageSize", "10"))
        offset = int(params.get("pageToken", "0") or 0)
        page = matched[offset : offset + page_size]
        body: dict[str, Any] = {"studies": page}
        if params.get("countTotal") == "true":
            body["totalCount"] = len(matched)
        if offset + page_size < len(matched):
            body["nextPageToken"] = str(offset + page_size)
        return httpx.Response(200, json=body)

    @staticmethod
    def _matches(study: dict[str, Any], params: dict[str, str]) -> bool:
        if "query.cond" in params and not _matches_term(study, params["query.cond"], "conditionsModule"):
            return False
        if "query.intr" in params and not _matches_term(study, params["query.intr"], "armsInterventionsModule"):
            return False
        if "query.spons" in params and not _matches_term(study, params["query.spons"], "sponsorCollaboratorsModule"):
            return False
        if "query.term" in params and not _matches_term(study, params["query.term"]):
            return False
        if "filter.overallStatus" in params:
            allowed = params["filter.overallStatus"].split(",")
            if _module(study, "statusModule").get("overallStatus") not in allowed:
                return False
        if "filter.advanced" in params and not _eval_advanced(study, params["filter.advanced"]):
            return False
        return True


@pytest.fixture
def studies() -> list[dict[str, Any]]:
    return load_studies()


@pytest.fixture
def fake_api(studies) -> FakeCTGov:
    return FakeCTGov(studies)


@pytest.fixture
def client(fake_api) -> CTGovClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_api.handler), base_url="https://fake.test")
    return CTGovClient(base_url="https://fake.test/api/v2", http_client=http, cache_ttl_seconds=0)


@pytest.fixture
def pipeline(client) -> Pipeline:
    return Pipeline(client=client, planner=Planner(llm=None))
