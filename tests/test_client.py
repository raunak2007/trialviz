import httpx
import pytest

from tests.conftest import FakeCTGov
from trialviz.ctgov.client import CallLog, CTGovClient, CTGovError
from trialviz.schemas.plan import EntityFilter


async def test_pagination_and_cap(fake_api, client):
    log = CallLog()
    result = await client.fetch_studies(EntityFilter(conditions=["melanoma"]), max_records=5, log=log)
    assert len(result.studies) == 5
    assert result.total_count == 12
    assert result.truncated is True
    assert len(log.calls) == 1 and log.calls[0].total_count == 12
    # most recent first
    assert result.studies[0]["protocolSection"]["identificationModule"]["nctId"] == "NCT05000011"


async def test_fetch_all_pages(fake_api, client):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_api.handler), base_url="https://fake.test")
    small_pages = CTGovClient(base_url="https://fake.test/api/v2", http_client=http, cache_ttl_seconds=0)
    log = CallLog()
    # page size is capped by max_records, so ask for many and let pages of 1000 be reduced by the fake to 10 per request
    result = await small_pages.fetch_studies(EntityFilter(conditions=["melanoma"]), max_records=100, log=log)
    assert len(result.studies) == 12 and result.truncated is False
    assert [c.studies_returned for c in log.calls] == [12]


async def test_retries_on_429_then_succeeds(fake_api, client):
    fake_api.fail_statuses = [429, 503]
    client._backoff = lambda attempt, retry_after=None: 0.0  # type: ignore[method-assign]
    result = await client.fetch_studies(EntityFilter(drugs=["pembrolizumab"]), max_records=10)
    assert len(result.studies) > 0
    assert len(fake_api.requests) == 3


async def test_bad_request_is_not_retried(fake_api, client):
    fake_api.fail_statuses = [400]
    with pytest.raises(CTGovError) as exc:
        await client.fetch_studies(EntityFilter(drugs=["pembrolizumab"]), max_records=10)
    assert exc.value.status == 400
    assert len(fake_api.requests) == 1


async def test_cache_serves_repeat_requests(fake_api):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_api.handler), base_url="https://fake.test")
    cached = CTGovClient(base_url="https://fake.test/api/v2", http_client=http, cache_ttl_seconds=60)
    log = CallLog()
    await cached.count_studies(EntityFilter(conditions=["melanoma"]), log=log)
    await cached.count_studies(EntityFilter(conditions=["melanoma"]), log=log)
    assert len(fake_api.requests) == 1
    assert [c.cached for c in log.calls] == [False, True]


async def test_count_studies_uses_total_count(client):
    assert await client.count_studies(EntityFilter(conditions=["melanoma"]), extra_advanced="AREA[Phase]PHASE3") == 4


async def test_connection_errors_surface_as_ctgov_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="https://fake.test")
    flaky = CTGovClient(base_url="https://fake.test/api/v2", http_client=http, max_attempts=2)
    flaky._backoff = lambda attempt, retry_after=None: 0.0  # type: ignore[method-assign]
    with pytest.raises(CTGovError):
        await flaky.count_studies(EntityFilter(conditions=["x"]))


def test_fake_api_evaluates_essie(studies):
    fake = FakeCTGov(studies)
    params = {"filter.advanced": "AREA[Phase]PHASE2 AND NOT AREA[Phase]PHASE1 AND NOT AREA[Phase]PHASE3"}
    matched = [s for s in studies if fake._matches(s, params)]
    assert len(matched) == 4
