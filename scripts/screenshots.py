"""Take README screenshots of the demo page with a headless browser.

Usage:
    pip install playwright && python -m playwright install chromium       # once
    python scripts/screenshots.py                                           # against http://localhost:8000
    python scripts/screenshots.py --base-url https://<workspace>--trialviz-web.modal.run

Writes docs/screenshots/*.png and inserts a "What it looks like" section with the images at the
top of README.md (before "## Contents") unless one is already there or --no-readme is given.
The queries go through the page's own fetch/render code, so the pictures are what a user sees.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"

SHOTS = [
    {
        "name": "phase-distribution",
        "request": {"query": "How are melanoma trials distributed across phases?"},
        "cite": "row:Phase 3",
        "caption": "Melanoma trials by phase. Every bar is exact (all records fetched) and the panel on the left "
        "shows the registry records behind the Phase 3 bar, with the field values that put them there.",
    },
    {
        "name": "combination-network",
        "request": {"query": "Which drugs frequently co-occur in combination studies for lung cancer?", "citations_per_datum": 3},
        "cite": "edge:0",
        "caption": "Drugs given in combination in lung cancer trials. An edge means both drugs are listed in the "
        "same arm group; the heaviest edge is selected and its supporting trials quote the arm entries.",
    },
    {
        "name": "partition-comparison",
        "request": {
            "query": "Compare phases for trials involving nivolumab vs ipilimumab",
            "comparison_mode": "partition",
            "citations_per_datum": 3,
        },
        "cite": "row:Both",
        "caption": "Nivolumab versus ipilimumab by phase with comparison_mode=partition: each trial is counted once, "
        "in 'nivolumab only', 'ipilimumab only', or 'Both'.",
    },
    {
        "name": "verification",
        "request": {"query": "How are melanoma trials distributed across phases?"},
        "element": "#meta",
        "caption": "The meta panel for the first chart: how the question was read, what the numbers mean, retrieval "
        "coverage, and the verification report that re-derived every value from the records.",
    },
]

FIELDS = {
    "drug_name": "drug",
    "condition": "condition",
    "citations_per_datum": "citations",
    "max_trials": "max_trials",
    "complete_up_to": "complete_up_to",
    "count_mode": "count_mode",
    "planner": "planner",
    "comparison_mode": "comparison_mode",
    "drug_match": "drug_match",
}
DEFAULTS = {
    "drug": "",
    "condition": "",
    "citations": "5",
    "max_trials": "2000",
    "complete_up_to": "5000",
    "count_mode": "auto",
    "planner": "auto",
    "comparison_mode": "overlapping",
    "drug_match": "any",
}

JS_CITE = """
([kind, key]) => {
  const viz = window.__last.visualization;
  if (kind === "edge") {
    const e = viz.data.edges[+key];
    const label = id => (viz.data.nodes.find(n => n.id === id) || { label: id }).label;
    showCitations(e, viz, `${label(e.source)} ↔ ${label(e.target)}`);
    return true;
  }
  const x = viz.encoding.x.field;
  const i = viz.data.findIndex(r => r[x] === key || r.series === key);
  const row = viz.data.find(r => (r[x] === key || r.series === key) && r.citations.length) || viz.data[Math.max(i, 0)];
  showCitations(row, viz);
  return true;
}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--no-readme", action="store_true", help="do not touch README.md")
    parser.add_argument("--scale", type=float, default=2.0, help="device scale factor (2 = retina-sharp images)")
    args = parser.parse_args()
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed: pip install playwright && python -m playwright install chromium")

    args.out.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1360, "height": 880}, device_scale_factor=args.scale)
        page.goto(base + "/?noautorun", wait_until="networkidle")
        if page.evaluate("typeof vegaEmbed === 'undefined' || typeof d3 === 'undefined'"):
            sys.exit("the chart libraries did not load from the CDN in the headless browser; check network access")
        for shot in SHOTS:
            print(f"{shot['name']}: {shot['request']['query']}", flush=True)
            # Drive the page's own controls so the screenshot is exactly what a user gets.
            page.fill("#query", shot["request"]["query"])
            page.evaluate("() => { const o = document.querySelector('details.opts'); if (o) o.open = true; }")
            for key, element in FIELDS.items():
                value = str(shot["request"].get(key, DEFAULTS[element]))
                if page.locator(f"#{element}").evaluate("el => el.tagName") == "SELECT":
                    page.select_option(f"#{element}", value)
                else:
                    page.fill(f"#{element}", value)
            page.evaluate("() => { const o = document.querySelector('details.opts'); if (o) o.open = false; }")
            page.click("#run")
            try:
                page.wait_for_selector("#chart canvas, #chart svg, #network svg, #error .error", timeout=120000)
                page.wait_for_function("() => !document.getElementById('run').disabled", timeout=120000)
            except PlaywrightTimeoutError:
                print("  no chart or error after 120 s; taking the screenshot anyway")
            if page.locator("#error .error").count():
                print("  request failed:", page.inner_text("#error")[:300])
                continue
            page.wait_for_timeout(3500 if page.locator("#network svg").count() else 600)  # let a force layout settle
            if shot.get("cite"):
                kind, key = shot["cite"].split(":", 1)
                page.evaluate(JS_CITE, [kind, key])
                page.wait_for_timeout(300)
            page.evaluate("window.scrollTo(0, 0)")
            path = args.out / f"{shot['name']}.png"
            if shot.get("element"):
                page.locator(shot["element"]).screenshot(path=str(path))
            else:
                page.screenshot(path=str(path), full_page=False)
            print(f"  wrote {path.relative_to(ROOT)}")
        browser.close()

    if args.no_readme:
        return
    readme = ROOT / "README.md"
    text = readme.read_text()
    if "## What it looks like" in text:
        print("README already has a screenshot section; left as is")
        return
    block = ["## What it looks like", ""]
    for shot in SHOTS:
        rel = f"docs/screenshots/{shot['name']}.png"
        if (ROOT / rel).exists():
            block += [f"![{shot['caption']}]({rel})", "", f"*{shot['caption']}*", ""]
    marker = "## Contents"
    if marker not in text:
        sys.exit("README.md has no '## Contents' heading to insert before")
    readme.write_text(text.replace(marker, "\n".join(block) + "\n" + marker, 1))
    print("inserted the screenshot section into README.md")


if __name__ == "__main__":
    main()
