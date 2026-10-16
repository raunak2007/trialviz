"""Score the planners against evals/planner_questions.jsonl.

Usage:
    python scripts/eval_planner.py                 # rules planner only (no key needed)
    python scripts/eval_planner.py --planner llm   # model planner, needs ANTHROPIC_API_KEY
    python scripts/eval_planner.py --planner both  # both, side by side

Each question carries the plan a correct planner should produce. Two scores are reported:

* plan: every expected field matches after normalization (analysis, dimension or network or numeric
  fields, measure, granularity, top_n, the base filter, and each comparison series). Entity names
  must be in the right filter field, so a drug filed under free-text `terms` is a miss.
* intent: the analysis type, the grouping, and the set of entity names are right, whichever field
  the names ended up in. This is the score that decides whether the right chart comes back.

Results are written to evals/results-<set>-<planner>.json and evals/results-<set>.md, where <set> is
"main" for planner_questions.jsonl and "heldout" for planner_questions_heldout.jsonl. The run is pinned
to 2026-10-15 as "today" so relative year ranges are stable.

The main set was used while developing the rule-based planner, so its rules score is in-sample. The
held-out set was written afterwards and the rules were not changed in response to it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trialviz.config import Settings  # noqa: E402
from trialviz.interpret.llm import AnthropicPlanner  # noqa: E402
from trialviz.interpret.normalize import PlanError  # noqa: E402
from trialviz.interpret.planner import Planner  # noqa: E402
from trialviz.schemas.plan import EntityFilter, QueryPlan  # noqa: E402
from trialviz.schemas.request import QueryRequest  # noqa: E402

QUESTIONS = ROOT / "evals" / "planner_questions.jsonl"
TODAY = date(2026, 10, 15)
ALWAYS_COMPARED_LISTS = ("drugs", "conditions", "sponsors", "countries", "terms", "phases", "statuses")
OPTIONAL_FILTER_FIELDS = ("study_types", "start_year", "end_year")


def _norm_list(values: list[str]) -> list[str]:
    return sorted(v.strip().casefold() for v in values)


def _filter_mismatches(expected: dict[str, Any], actual: EntityFilter, strict: bool) -> list[str]:
    """Fields of the base filter that differ. Optional fields are compared only when the expectation names them."""
    out: list[str] = []
    got = actual.model_dump(mode="json")
    for key in ALWAYS_COMPARED_LISTS:
        if strict and _norm_list(expected.get(key, [])) != _norm_list(got.get(key, [])):
            out.append(f"{key}: expected {expected.get(key, [])}, got {got.get(key, [])}")
    for key in OPTIONAL_FILTER_FIELDS:
        if key not in expected or expected[key] == "*":
            continue
        exp, act = expected[key], got.get(key)
        if isinstance(exp, list):
            if _norm_list(exp) != _norm_list(act or []):
                out.append(f"{key}: expected {exp}, got {act}")
        elif exp != act:
            out.append(f"{key}: expected {exp}, got {act}")
    return out


def _entity_names(f: EntityFilter) -> set[str]:
    return {v.strip().casefold() for key in ("drugs", "conditions", "sponsors", "countries", "terms") for v in getattr(f, key)}


def _expected_entity_names(expect: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for source in [expect.get("filter", {}), *expect.get("series", [])]:
        for key in ("drugs", "conditions", "sponsors", "countries", "terms"):
            names |= {v.strip().casefold() for v in source.get(key, [])}
    return names


def score(expect: dict[str, Any], plan: QueryPlan | None, error: PlanError | None) -> dict[str, Any]:
    """Return {'plan': bool, 'intent': bool, 'mismatches': [...]} for one question."""
    mismatches: list[str] = []
    if expect["analysis"] == "unsupported":
        ok = error is not None and error.code == "unsupported_query"
        if not ok:
            mismatches.append(f"expected unsupported, got {plan.analysis.value if plan else (error.code if error else '?')}")
        return {"plan": ok, "intent": ok, "mismatches": mismatches}
    if plan is None:
        return {
            "plan": False,
            "intent": False,
            "mismatches": [f"planner error: {error.code}: {error.message}" if error else "no plan"],
        }

    intent_ok = plan.analysis.value == expect["analysis"]
    if not intent_ok:
        mismatches.append(f"analysis: expected {expect['analysis']}, got {plan.analysis.value}")

    def check(name: str, got: Any) -> None:
        nonlocal intent_ok
        if name in expect and expect[name] != got:
            mismatches.append(f"{name}: expected {expect[name]}, got {got}")
            if name in ("dimension", "network", "split_by"):
                intent_ok = False

    check("dimension", plan.dimension.value if plan.dimension else None)
    check("split_by", plan.split_by.value if plan.split_by else None)
    check("network", plan.network.value if plan.network else None)
    check("measure", plan.measure.value)
    check("time_granularity", plan.time_granularity.value)
    check("top_n", plan.top_n)
    check("numeric_x", plan.numeric_x.value if plan.numeric_x else None)
    if "numeric" in expect:
        got = sorted(v.value for v in (plan.numeric_x, plan.numeric_y) if v)
        if got != sorted(expect["numeric"]):
            mismatches.append(f"numeric axes: expected {sorted(expect['numeric'])}, got {got}")
            intent_ok = False

    mismatches += _filter_mismatches(expect.get("filter", {}), plan.filter, strict=True)

    if "series" in expect:
        exp_series = expect["series"]
        if len(plan.series) != len(exp_series):
            mismatches.append(f"series: expected {len(exp_series)}, got {len(plan.series)}")
        else:
            remaining = list(plan.series)
            for exp in exp_series:
                match = next((s for s in remaining if not _filter_mismatches(exp, s.filter, strict=True)), None)
                if match is None:
                    mismatches.append(
                        f"series {exp} has no matching series among {[s.filter.model_dump(exclude_defaults=True) for s in plan.series]}"
                    )
                else:
                    remaining.remove(match)

    names = _entity_names(plan.filter)
    for s in plan.series:
        names |= _entity_names(s.filter)
    expected_names = _expected_entity_names(expect)
    if names != expected_names:
        mismatches.append(f"entities: expected {sorted(expected_names)}, got {sorted(names)}")
        intent_ok = False
    return {"plan": not mismatches, "intent": intent_ok, "mismatches": mismatches}


async def run(planner_mode: str, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = Settings.from_env()
    llm = None
    if planner_mode == "llm":
        if not settings.anthropic_api_key:
            sys.exit("ANTHROPIC_API_KEY is not set; the llm planner cannot be evaluated")
        llm = AnthropicPlanner(model=settings.model, api_key=settings.anthropic_api_key)
    planner = Planner(llm)
    rows = []
    for item in questions:
        structured = QueryRequest.model_validate({"query": item["query"], **item.get("structured", {})}).as_filter()
        plan: QueryPlan | None = None
        error: PlanError | None = None
        info = None
        try:
            outcome = await planner.plan(item["query"], structured, mode=planner_mode, today=TODAY)  # type: ignore[arg-type]
            plan, info = outcome.plan, outcome.info
        except PlanError as exc:
            error = exc
        result = score(item["expect"], plan, error)
        rows.append(
            {
                "id": item["id"],
                "query": item["query"],
                "plan_match": result["plan"],
                "intent_match": result["intent"],
                "mismatches": result["mismatches"],
                "planner": info.model_dump() if info else None,
                "plan": plan.model_dump(mode="json", exclude={"interpretation", "assumptions"}) if plan else None,
                "error": {"code": error.code, "message": error.message} if error else None,
            }
        )
        mark = "ok " if result["plan"] else ("~  " if result["intent"] else "MISS")
        print(f"{mark} {item['id']:6} {item['query'][:70]}")
        for m in result["mismatches"]:
            print(f"         {m}")
    return rows


def summarize(mode: str, rows: list[dict[str, Any]], model: str | None) -> str:
    n = len(rows)
    plan_ok = sum(r["plan_match"] for r in rows)
    intent_ok = sum(r["intent_match"] for r in rows)
    by_group: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_group.setdefault(r["id"].split("-")[0], []).append(r)
    lines = [
        f"## {mode}{f' ({model})' if model else ''}",
        "",
        f"- plan matches: **{plan_ok}/{n}**",
        f"- intent matches (right analysis, grouping, and entities): **{intent_ok}/{n}**",
        "",
        "| group | plan | intent |",
        "|---|---|---|",
    ]
    names = {
        "tt": "time trends",
        "di": "distributions",
        "cp": "comparisons",
        "ge": "geographic",
        "nw": "networks",
        "sc": "scatter",
        "hi": "histograms",
        "un": "unsupported",
    }
    for key, items in by_group.items():
        lines.append(
            f"| {names.get(key, key)} | {sum(i['plan_match'] for i in items)}/{len(items)} | {sum(i['intent_match'] for i in items)}/{len(items)} |"
        )
    misses = [r for r in rows if not r["plan_match"]]
    if misses:
        lines += ["", "Misses:", ""]
        for r in misses:
            lines.append(f"- `{r['id']}` {r['query']}")
            for m in r["mismatches"]:
                lines.append(f"  - {m}")
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--planner", choices=["rules", "llm", "both"], default="rules")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    args = parser.parse_args()
    questions_path = args.questions.resolve()
    questions = [json.loads(line) for line in questions_path.read_text().splitlines() if line.strip()]
    modes = ["rules", "llm"] if args.planner == "both" else [args.planner]
    set_name = questions_path.stem.replace("planner_questions", "").strip("_") or "main"
    report = [
        f"# Planner evaluation ({set_name} set)\n\n{len(questions)} questions from `{questions_path.name}`, today pinned to {TODAY}.\n"
    ]
    for mode in modes:
        print(f"\n=== {mode} ===")
        rows = await run(mode, questions)
        model = next((r["planner"]["model"] for r in rows if r["planner"] and r["planner"].get("model")), None)
        used_fallback = [r["id"] for r in rows if r["planner"] and r["planner"]["mode"] != mode]
        out = ROOT / "evals" / f"results-{set_name}-{mode}.json"
        out.write_text(json.dumps(rows, indent=2))
        section = summarize(mode, rows, model)
        if used_fallback:
            section += f"\nQuestions answered by the fallback planner instead of the model: {', '.join(used_fallback)}\n"
        report.append(section)
        print(section)
    report_path = ROOT / "evals" / f"results-{set_name}.md"
    report_path.write_text("\n".join(report))
    print(f"wrote {report_path.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
