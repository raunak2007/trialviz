"""Run the example queries and save the real JSON outputs.

Usage:
    python scripts/run_examples.py                      # in-process, live ClinicalTrials.gov, planner from env
    python scripts/run_examples.py --planner rules      # force the deterministic planner
    python scripts/run_examples.py --base-url http://localhost:8000   # go through a running server instead

Outputs land in examples/outputs/<name>.json (request + response, or request + error) and a
summary table is written to examples/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trialviz.app import build_pipeline  # noqa: E402
from trialviz.config import Settings  # noqa: E402
from trialviz.ctgov.client import CTGovError  # noqa: E402
from trialviz.interpret.normalize import PlanError  # noqa: E402
from trialviz.pipeline import NoDataError  # noqa: E402
from trialviz.schemas.request import QueryRequest  # noqa: E402


async def run_in_process(requests: list[dict], planner: str | None) -> list[dict]:
    settings = Settings.from_env()
    pipeline = build_pipeline(settings)
    results = []
    try:
        for req in requests:
            body = dict(req)
            if planner:
                body["planner"] = planner
            try:
                response = await pipeline.run(QueryRequest.model_validate(body))
                results.append({"status": 200, "body": response.model_dump(mode="json")})
            except PlanError as exc:
                results.append(
                    {"status": 422, "body": {"error": {"code": exc.code, "message": exc.message, "suggestions": exc.suggestions}}}
                )
            except NoDataError as exc:
                results.append({"status": 404, "body": {"error": {"code": "no_data", "message": exc.message}}})
            except CTGovError as exc:
                results.append({"status": 502, "body": {"error": {"code": "upstream_error", "message": str(exc)}}})
    finally:
        await pipeline.client.aclose()
    return results


async def run_against_server(requests: list[dict], base_url: str, planner: str | None) -> list[dict]:
    results = []
    async with httpx.AsyncClient(base_url=base_url, timeout=300) as http:
        for req in requests:
            body = dict(req)
            if planner:
                body["planner"] = planner
            r = await http.post("/query", json=body)
            results.append({"status": r.status_code, "body": r.json()})
    return results


def summarize(name: str, request: dict, result: dict) -> dict:
    body = result["body"]
    if result["status"] != 200:
        err = body.get("error", {})
        return {
            "name": name,
            "status": result["status"],
            "type": "error: " + err.get("code", "?"),
            "detail": err.get("message", "")[:120],
        }
    viz, meta = body["visualization"], body["meta"]
    if viz["type"] == "network_graph":
        size = f"{len(viz['data']['nodes'])} nodes, {len(viz['data']['edges'])} edges"
    else:
        size = f"{len(viz['data'])} rows"
    retrieval = "; ".join(
        f"{r['label']}: {r['fetched']} of {r['total_matching']} fetched ({r['count_method']})" for r in meta["retrieval"]
    )
    return {
        "name": name,
        "status": 200,
        "type": viz["type"],
        "title": viz["title"],
        "size": size,
        "planner": meta["planner"]["mode"],
        "retrieval": retrieval,
        "api_calls": len(meta["api_calls"]),
        "total_ms": meta["timing"]["total_ms"],
    }


def write_summary(rows: list[dict], out_dir: Path) -> None:
    lines = [
        "# Example runs",
        "",
        "Produced by `python scripts/run_examples.py` against the live ClinicalTrials.gov API.",
        "Each file in `outputs/` holds the request that was sent and the exact response body.",
        "",
        "| # | file | HTTP | type | title | size | planner | retrieval | API calls | total ms |",
        "|---|------|------|------|-------|------|---------|-----------|-----------|----------|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | `outputs/{r['name']}.json` | {r['status']} | {r['type']} | {r.get('title', r.get('detail', ''))} | "
            f"{r.get('size', '')} | {r.get('planner', '')} | {r.get('retrieval', '')} | {r.get('api_calls', '')} | {r.get('total_ms', '')} |"
        )
    (out_dir.parent / "README.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="POST to a running server instead of calling the pipeline in-process")
    parser.add_argument("--planner", choices=["auto", "llm", "rules"], help="override the planner mode for every example")
    parser.add_argument("--queries", default=str(ROOT / "examples" / "queries.json"))
    parser.add_argument("--out", default=str(ROOT / "examples" / "outputs"))
    parser.add_argument("--only", help="run a single example by name")
    args = parser.parse_args()

    examples = json.loads(Path(args.queries).read_text())
    if args.only:
        examples = [e for e in examples if e["name"] == args.only]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    requests = [e["request"] for e in examples]

    if args.base_url:
        results = asyncio.run(run_against_server(requests, args.base_url, args.planner))
    else:
        results = asyncio.run(run_in_process(requests, args.planner))

    rows = []
    for example, result in zip(examples, results, strict=True):
        path = out_dir / f"{example['name']}.json"
        path.write_text(
            json.dumps({"request": example["request"], "http_status": result["status"], "response": result["body"]}, indent=2)
        )
        row = summarize(example["name"], example["request"], result)
        rows.append(row)
        print(f"{row['status']}  {example['name']:<42} {row['type']:<22} {row.get('size', row.get('detail', ''))}")
    if not args.only:
        write_summary(rows, out_dir)
        print(f"\nwrote {len(rows)} outputs to {out_dir} and a summary to {out_dir.parent / 'README.md'}")


if __name__ == "__main__":
    main()
