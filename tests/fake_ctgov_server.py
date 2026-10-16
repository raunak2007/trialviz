"""Serve the recorded fixture over HTTP as a stand-in for ClinicalTrials.gov.

Lets the whole service (including the demo page) run with no internet access:

    python -m tests.fake_ctgov_server --port 8600
    TRIALVIZ_CTGOV_BASE_URL=http://localhost:8600/api/v2 uvicorn trialviz.app:app --port 8000

The query semantics are the subset implemented by `tests.conftest.FakeCTGov`.
"""

from __future__ import annotations

import argparse
import json

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from tests.conftest import FakeCTGov, load_studies


def create_fake_app() -> FastAPI:
    fake = FakeCTGov(load_studies())
    app = FastAPI(title="fake ClinicalTrials.gov")

    @app.get("/api/v2/studies")
    async def studies(request: Request) -> Response:
        proxied = httpx.Request("GET", str(request.url))
        reply = fake.handler(proxied)
        return Response(content=json.dumps(reply.json()), status_code=reply.status_code, media_type="application/json")

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8600)
    args = parser.parse_args()
    uvicorn.run(create_fake_app(), host="127.0.0.1", port=args.port)
