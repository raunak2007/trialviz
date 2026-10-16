"""Async HTTP client for the ClinicalTrials.gov v2 API.

Responsibilities: pagination with a hard cap, retries with backoff on 429 and
5xx, a bounded in-memory TTL cache, a concurrency limit so bucket-count fan-outs
stay polite, and a per-run call log so every number in a response can be traced
to the exact URLs that produced it.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

from trialviz.ctgov.params import build_count_params, build_search_params
from trialviz.schemas.plan import EntityFilter
from trialviz.schemas.response import ApiCall

API_BASE = "https://clinicaltrials.gov/api/v2"
MAX_PAGE_SIZE = 1000
RETRY_STATUSES = {429, 500, 502, 503, 504}


class CTGovError(Exception):
    """The API could not be used: bad request, repeated failures, or malformed data."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass
class CallLog:
    """Collects the API calls made while answering one query."""

    calls: list[ApiCall] = field(default_factory=list)

    def add(self, call: ApiCall) -> None:
        self.calls.append(call)


@dataclass
class FetchResult:
    studies: list[dict[str, Any]]
    total_count: int | None
    truncated: bool


class _TTLCache:
    def __init__(self, ttl_seconds: float, max_entries: int):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def get(self, key: str) -> dict[str, Any] | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)


class CTGovClient:
    def __init__(
        self,
        *,
        base_url: str = API_BASE,
        timeout_seconds: float = 60.0,
        max_attempts: int = 4,
        max_concurrency: int = 4,
        cache_ttl_seconds: float = 3600.0,
        cache_entries: int = 256,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cache = _TTLCache(cache_ttl_seconds, cache_entries)
        self._http = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"Accept": "application/json", "User-Agent": "trialviz/0.1 (+https://clinicaltrials.gov/data-api/api)"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ pages

    async def get_json(self, path: str, params: dict[str, str], log: CallLog | None = None) -> dict[str, Any]:
        """GET one endpoint with retries; returns the parsed JSON body."""
        url = f"{self.base_url}{path}"
        cache_key = str(httpx.URL(url, params=params))
        cached = self._cache.get(cache_key)
        if cached is not None:
            if log is not None:
                log.add(self._describe(cache_key, 200, 0, cached, cached=True))
            return cached

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            try:
                async with self._semaphore:
                    response = await self._http.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise CTGovError(f"ClinicalTrials.gov unreachable after {attempt} attempts: {exc}", url=cache_key) from exc

            elapsed_ms = int((time.monotonic() - started) * 1000)
            if response.status_code in RETRY_STATUSES and attempt < self.max_attempts:
                await asyncio.sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                continue
            if response.status_code != 200:
                if log is not None:
                    log.add(ApiCall(url=cache_key, status=response.status_code, elapsed_ms=elapsed_ms))
                raise CTGovError(
                    f"ClinicalTrials.gov returned HTTP {response.status_code}: {response.text[:300]}",
                    status=response.status_code,
                    url=cache_key,
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise CTGovError("ClinicalTrials.gov returned a non-JSON body", status=200, url=cache_key) from exc
            if not isinstance(body, dict):
                raise CTGovError("ClinicalTrials.gov returned an unexpected JSON shape", status=200, url=cache_key)
            self._cache.put(cache_key, body)
            if log is not None:
                log.add(self._describe(cache_key, response.status_code, elapsed_ms, body))
            return body

        raise CTGovError(f"ClinicalTrials.gov request failed: {last_error}", url=cache_key)

    @staticmethod
    def _describe(url: str, status: int, elapsed_ms: int, body: dict[str, Any], cached: bool = False) -> ApiCall:
        studies = body.get("studies")
        return ApiCall(
            url=url,
            status=status,
            elapsed_ms=elapsed_ms,
            cached=cached,
            studies_returned=len(studies) if isinstance(studies, list) else None,
            total_count=body.get("totalCount") if isinstance(body.get("totalCount"), int) else None,
        )

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return min(0.5 * (2 ** (attempt - 1)), 8.0)

    # --------------------------------------------------------------- studies

    async def fetch_studies(
        self, f: EntityFilter, *, max_records: int, complete_up_to: int | None = None, log: CallLog | None = None
    ) -> FetchResult:
        """Page through /studies for a filter until `max_records` records or the end.

        Records come back sorted by StartDate descending, so a truncated
        result is 'the N most recently started matching trials', which is a
        sample a reader can reason about.

        With `complete_up_to`, the first page's `totalCount` decides the cap:
        when the filter matches at most that many trials, every record is
        fetched regardless of `max_records`. No extra request is needed for the
        decision because the count rides along with the first page.
        """
        page_size = min(MAX_PAGE_SIZE, max(1, max_records))
        params = build_search_params(f, page_size=page_size)
        studies: list[dict[str, Any]] = []
        total_count: int | None = None
        page_token: str | None = None
        cap = max_records

        while True:
            page_params = dict(params)
            if page_token:
                page_params["pageToken"] = page_token
            body = await self.get_json("/studies", page_params, log)
            if total_count is None and isinstance(body.get("totalCount"), int):
                total_count = body["totalCount"]
                if complete_up_to is not None and total_count <= complete_up_to:
                    cap = max(cap, total_count)
            page = body.get("studies") or []
            if not isinstance(page, list):
                raise CTGovError("`studies` is not a list in the API response")
            studies.extend(page)
            page_token = body.get("nextPageToken")
            if not page_token or len(studies) >= cap or not page:
                break

        if len(studies) > cap:
            studies = studies[:cap]
        truncated = total_count is not None and len(studies) < total_count
        if total_count is None and page_token:
            truncated = True
        return FetchResult(studies=studies, total_count=total_count, truncated=truncated)

    async def count_studies(self, f: EntityFilter, *, extra_advanced: str = "", log: CallLog | None = None) -> int:
        """Number of studies matching a filter plus an optional Essie clause, without downloading them."""
        params = build_count_params(f, extra_advanced=extra_advanced)
        body = await self.get_json("/studies", params, log)
        total = body.get("totalCount")
        if not isinstance(total, int):
            raise CTGovError("API response did not include totalCount")
        return total
