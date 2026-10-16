"""Measure a few properties of the live ClinicalTrials.gov v2 API that the design relies on.

Usage:
    python scripts/measure_api.py                 # all measurements, writes evals/api_measurements.md
    python scripts/measure_api.py --only 1,3      # a subset
    python scripts/measure_api.py --drug nivolumab --condition melanoma

Each measurement prints the exact request URLs it used, so every number can be reproduced with
curl. Nothing here is cached across runs; expect it to take a minute or two because the drug-match
and network measurements download a few thousand records.

Measurements:
  1. Drug search vs. listed intervention: how many `query.intr=<drug>` matches do not list the
     drug as an intervention (name, other name, MeSH term, or arm entry)?
  2. "Phase: Not Applicable" vs. no phase at all: AREA[Phase]NA versus records with an empty
     phases list (observational studies), across the whole registry.
  3. UNKNOWN status: how many registry records carry overallStatus UNKNOWN (last known status,
     no longer updated), and what share of the registry that is.
  4. query.spons vs. query.lead: a sponsor search counts collaborator roles too.
  5. Same-arm vs. same-trial drug pairs: of the drug pairs that co-occur in a trial, how many are
     never listed in the same arm (comparators, not combinations), and how many trials list no
     arm groups at all.
  6. Missing AREA[] prefix: `filter.advanced=PHASE3` is a free-text search, not a phase filter.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trialviz.analysis.networks import build_network  # noqa: E402
from trialviz.ctgov.client import CallLog, CTGovClient, CTGovError  # noqa: E402
from trialviz.ctgov.exact_counts import PHASE_BUCKET_EXPRESSIONS  # noqa: E402
from trialviz.ctgov.params import STUDY_FIELDS  # noqa: E402
from trialviz.ctgov.records import parse_studies  # noqa: E402
from trialviz.schemas.enums import NetworkKind, StudyType  # noqa: E402
from trialviz.schemas.plan import EntityFilter  # noqa: E402

OUT = ROOT / "evals" / "api_measurements.md"


@dataclass
class Finding:
    number: int
    title: str
    lines: list[str]
    urls: list[str]

    def markdown(self) -> str:
        body = "\n".join(f"- {line}" for line in self.lines)
        curls = "\n".join(f"curl -s '{u}'" for u in self.urls)
        return f"### {self.number}. {self.title}\n\n{body}\n\nReproduce:\n\n```\n{curls}\n```\n"


def _urls(log: CallLog) -> list[str]:
    seen: list[str] = []
    for c in log.calls:
        if c.url not in seen:
            seen.append(c.url)
    return seen


async def probe(client: CTGovClient) -> None:
    """Fail fast if the projection the service uses is rejected by the API."""
    log = CallLog()
    try:
        await client.fetch_studies(EntityFilter(conditions=["melanoma"]), max_records=1, log=log)
    except CTGovError as exc:
        sys.exit(f"the field projection used by the service was rejected: {exc}\nfields: {','.join(STUDY_FIELDS)}")
    print(f"probe ok: {log.calls[0].url}\n")


async def m1_drug_match(client: CTGovClient, drug: str, cap: int) -> Finding:
    log = CallLog()
    result = await client.fetch_studies(EntityFilter(drugs=[drug]), max_records=cap, complete_up_to=cap, log=log)
    trials = parse_studies(result.studies)
    listed = [t for t in trials if t.mentions_drug(drug)]
    mention_only = [t for t in trials if not t.mentions_drug(drug)]
    examples = ", ".join(f"{t.nct_id} ({(t.brief_title or '')[:60]})" for t in mention_only[:3]) or "none"
    share = 100 * len(mention_only) / len(trials) if trials else 0
    return Finding(
        1,
        f"query.intr={drug}: matches that do not list the drug as an intervention",
        [
            f"{result.total_count:,} trials match `query.intr={drug}`; {len(trials):,} fetched"
            + (" (all of them)" if not result.truncated else " (most recently started)"),
            f"{len(listed):,} list it as an intervention by name, other name, MeSH term, or arm entry; "
            f"{len(mention_only):,} ({share:.1f}%) only mention it elsewhere in the intervention area",
            f"examples of mention-only matches: {examples}",
            "consequence: `meta.retrieval[].drug_match` reports both counts on every response; `drug_match=strict` drops the mention-only trials",
        ],
        _urls(log)[:2],
    )


async def m2_phase_na(client: CTGovClient) -> Finding:
    log = CallLog()
    total = await client.count_studies(EntityFilter(), log=log)
    na = await client.count_studies(EntityFilter(), extra_advanced="AREA[Phase]NA", log=log)
    buckets = {
        label: await client.count_studies(EntityFilter(), extra_advanced=expr, log=log)
        for label, expr in PHASE_BUCKET_EXPRESSIONS
    }
    with_phase = sum(buckets.values())
    observational = await client.count_studies(EntityFilter(study_types=[StudyType.OBSERVATIONAL]), log=log)
    return Finding(
        2,
        "An explicit 'Not Applicable' phase is not the same as no phase",
        [
            f"{total:,} studies in the registry",
            f"{na:,} carry `AREA[Phase]NA` (an explicit 'Not Applicable', typical of device and behavioral trials)",
            f"{with_phase:,} are covered by the mutually exclusive phase buckets the service uses "
            f"({', '.join(f'{k}: {v:,}' for k, v in buckets.items())})",
            f"{total - with_phase:,} have no phase value at all; {observational:,} are observational studies, which never have one",
            "consequence: phase charts restrict to interventional studies and label the remainder 'Not specified' rather than folding it into 'Not Applicable'",
        ],
        _urls(log)[:4],
    )


async def m3_unknown_status(client: CTGovClient) -> Finding:
    from trialviz.schemas.enums import OverallStatus

    log = CallLog()
    total = await client.count_studies(EntityFilter(), log=log)
    unknown = await client.count_studies(EntityFilter(statuses=[OverallStatus.UNKNOWN]), log=log)
    recruiting = await client.count_studies(EntityFilter(statuses=[OverallStatus.RECRUITING]), log=log)
    return Finding(
        3,
        "UNKNOWN status: records whose last known status was never updated",
        [
            f"{unknown:,} of {total:,} studies ({100 * unknown / total:.1f}%) have overallStatus UNKNOWN",
            f"for scale, {recruiting:,} are RECRUITING",
            "consequence: status charts show 'Unknown status' as its own bar instead of dropping those trials, and a "
            "'recruiting' filter never includes them",
        ],
        _urls(log),
    )


async def m4_spons_vs_lead(client: CTGovClient, sponsor: str) -> Finding:
    log = CallLog()
    spons = await client.count_studies(EntityFilter(sponsors=[sponsor]), log=log)
    body = await client.get_json(
        "/studies", {"format": "json", "query.lead": sponsor, "pageSize": "1", "countTotal": "true"}, log=log
    )
    lead = body.get("totalCount")
    return Finding(
        4,
        f"query.spons vs query.lead for '{sponsor}'",
        [
            f"`query.spons={sponsor}` (lead sponsor or collaborator): {spons:,} trials",
            f"`query.lead={sponsor}` (lead sponsor only): {lead:,} trials"
            if isinstance(lead, int)
            else f"`query.lead` failed: {body}",
            "consequence: the service searches with query.spons, so 'trials sponsored by X' includes trials where X is a "
            "collaborator; the sponsor-to-collaborator network separates the two roles, and the response says which "
            "search area was used in meta.api_calls",
        ],
        _urls(log),
    )


async def m5_same_arm(client: CTGovClient, condition: str, cap: int) -> Finding:
    log = CallLog()
    f = EntityFilter(conditions=[condition], study_types=[StudyType.INTERVENTIONAL])
    result = await client.fetch_studies(f, max_records=cap, complete_up_to=cap, log=log)
    trials = parse_studies(result.studies)
    same_trial = build_network(trials, NetworkKind.DRUG_DRUG_SAME_TRIAL, max_edges=10**6)
    same_arm = build_network(trials, NetworkKind.DRUG_DRUG, max_edges=10**6)
    arm_pairs = {frozenset((e.source, e.target)) for e in same_arm.edges}
    trial_pairs = {frozenset((e.source, e.target)) for e in same_trial.edges}
    comparator_only = trial_pairs - arm_pairs
    share = 100 * len(comparator_only) / len(trial_pairs) if trial_pairs else 0
    return Finding(
        5,
        f"Drug pairs in the same trial vs the same arm ({condition}, interventional)",
        [
            f"{result.total_count:,} trials match; {len(trials):,} fetched" + ("" if result.truncated else " (all of them)"),
            f"{len(trial_pairs):,} distinct drug pairs co-occur somewhere in a trial; {len(arm_pairs):,} of them are listed "
            f"together in at least one arm group",
            f"{len(comparator_only):,} pairs ({share:.1f}%) never share an arm: they are comparators or alternatives, not combinations",
            f"{same_arm.trials_without_arm_data:,} of {len(trials):,} fetched trials list no arm groups and cannot contribute to the same-arm network",
            "consequence: `drug_drug` links drugs only when they share an arm; `drug_drug_same_trial` is the looser graph, "
            "and the response names which one it built in `edge_semantics`",
        ],
        _urls(log)[:2],
    )


async def m6_area_prefix(client: CTGovClient, condition: str) -> Finding:
    log = CallLog()
    f = EntityFilter(conditions=[condition])
    scoped = await client.count_studies(f, extra_advanced="AREA[Phase]PHASE3", log=log)
    bare = await client.count_studies(f, extra_advanced="PHASE3", log=log)
    return Finding(
        6,
        f"A missing AREA[] prefix turns a phase filter into a free-text search ({condition})",
        [
            f"`filter.advanced=AREA[Phase]PHASE3`: {scoped:,} trials",
            f"`filter.advanced=PHASE3`: {bare:,} trials (matches the token anywhere in the record)",
            "consequence: every Essie clause the service emits is built by `ctgov.params.essie_area`, which always writes the AREA[] prefix",
        ],
        _urls(log),
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="", help="comma-separated measurement numbers, e.g. 1,5")
    parser.add_argument("--drug", default="pembrolizumab")
    parser.add_argument("--condition", default="melanoma")
    parser.add_argument("--sponsor", default="Pfizer")
    parser.add_argument("--cap", type=int, default=5000, help="records to download for measurements 1 and 5")
    parser.add_argument("--base-url", default=None, help="API base, e.g. http://localhost:8600/api/v2 for the fake server")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    wanted = {int(x) for x in args.only.split(",") if x.strip()} or {1, 2, 3, 4, 5, 6}

    client = CTGovClient(cache_ttl_seconds=0, **({"base_url": args.base_url} if args.base_url else {}))
    findings: list[Finding] = []
    try:
        await probe(client)
        steps = {
            1: lambda: m1_drug_match(client, args.drug, args.cap),
            2: lambda: m2_phase_na(client),
            3: lambda: m3_unknown_status(client),
            4: lambda: m4_spons_vs_lead(client, args.sponsor),
            5: lambda: m5_same_arm(client, args.condition, args.cap),
            6: lambda: m6_area_prefix(client, args.condition),
        }
        for number in sorted(wanted):
            print(f"measuring {number}...", flush=True)
            try:
                finding = await steps[number]()
            except CTGovError as exc:
                finding = Finding(number, f"measurement {number} failed", [str(exc)], [exc.url or ""])
            findings.append(finding)
            print(finding.markdown())
    finally:
        await client.aclose()

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    args.out.write_text(
        f"# Live API measurements\n\nMeasured on {stamp} against {client.base_url}.\n\n"
        + "\n".join(f.markdown() for f in findings)
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
