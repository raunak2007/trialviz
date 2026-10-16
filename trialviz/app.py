"""FastAPI application: POST /query plus health, schema, and a static demo page."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from trialviz.config import Settings
from trialviz.ctgov.client import CTGovClient, CTGovError
from trialviz.interpret.llm import AnthropicPlanner
from trialviz.interpret.normalize import PlanError
from trialviz.interpret.planner import Planner
from trialviz.pipeline import NoDataError, Pipeline
from trialviz.schemas.request import QueryRequest
from trialviz.schemas.response import ErrorDetail, ErrorResponse, QueryResponse
from trialviz.viz.validate import SpecInvalid

log = logging.getLogger("trialviz")

ROOT = Path(__file__).resolve().parent.parent
DEMO_PAGE = ROOT / "demo" / "index.html"
EXAMPLE_QUERIES = ROOT / "examples" / "queries.json"


def _error(
    status: int, code: str, message: str, suggestions: list[str] | None = None, details: dict[str, Any] | None = None
) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, suggestions=suggestions or [], details=details))  # type: ignore[arg-type]
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def build_pipeline(settings: Settings, *, client: CTGovClient | None = None) -> Pipeline:
    ctgov = client or CTGovClient(
        base_url=settings.ctgov_base_url,
        timeout_seconds=settings.http_timeout_seconds,
        max_concurrency=settings.max_concurrency,
        cache_ttl_seconds=settings.cache_ttl_seconds,
    )
    llm = AnthropicPlanner(model=settings.model, api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None
    return Pipeline(client=ctgov, planner=Planner(llm))


def create_app(settings: Settings | None = None, *, pipeline: Pipeline | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pipeline = pipeline or build_pipeline(settings)
        app.state.settings = settings
        planner_desc = (
            f"llm ({settings.model}) with rule fallback" if settings.anthropic_api_key else "rules only (no ANTHROPIC_API_KEY)"
        )
        log.info("planner: %s", planner_desc)
        try:
            yield
        finally:
            await app.state.pipeline.client.aclose()

    app = FastAPI(
        title="trialviz",
        version="0.1.0",
        description="Turns natural-language questions about clinical trials into visualization specifications backed by ClinicalTrials.gov.",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [f"{'.'.join(str(p) for p in e.get('loc', []) if p != 'body')}: {e.get('msg')}" for e in exc.errors()]
        return _error(422, "invalid_request", "The request body is invalid.", details={"problems": problems})

    @app.post(
        "/query",
        response_model=QueryResponse,
        responses={422: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
        summary="Answer a clinical-trial question with a visualization specification",
    )
    async def query(request: QueryRequest) -> Any:
        pipe: Pipeline = app.state.pipeline
        try:
            return await pipe.run(request)
        except PlanError as exc:
            return _error(422, exc.code, exc.message, exc.suggestions)
        except NoDataError as exc:
            return _error(404, "no_data", exc.message, details={"filters": [f.model_dump(mode="json") for f in exc.filters]})
        except CTGovError as exc:
            log.warning("upstream failure: %s", exc)
            return _error(502, "upstream_error", str(exc), details={"url": exc.url, "status": exc.status})
        except SpecInvalid as exc:
            log.error("spec validation failed: %s", exc)
            return _error(500, "internal_error", f"Internal consistency check failed: {exc}")

    @app.get("/health", summary="Liveness and planner configuration")
    async def health() -> dict[str, Any]:
        s: Settings = app.state.settings
        return {
            "status": "ok",
            "planner": "llm" if s.anthropic_api_key else "rules",
            "model": s.model if s.anthropic_api_key else None,
        }

    @app.get("/schema", summary="JSON Schemas for the request, response, and error envelopes")
    async def schema() -> dict[str, Any]:
        return {
            "request": QueryRequest.model_json_schema(),
            "response": QueryResponse.model_json_schema(),
            "error": ErrorResponse.model_json_schema(),
        }

    @app.get("/examples", summary="Example questions (used by the demo page)")
    async def examples() -> list[dict[str, Any]]:
        if not EXAMPLE_QUERIES.is_file():
            return []
        return json.loads(EXAMPLE_QUERIES.read_text())

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        if DEMO_PAGE.is_file():
            return FileResponse(DEMO_PAGE)
        return {"service": "trialviz", "docs": "/docs", "query": "POST /query"}

    return app


app = create_app()
