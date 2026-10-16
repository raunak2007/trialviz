"""Exact per-category counts computed by the API instead of from a sample.

When a filter matches more trials than we are willing to download, bar heights
derived from a truncated sample would be wrong. For dimensions whose categories
can be written as Essie expressions, we instead ask the API for one
`totalCount` per category. The sample is still fetched for citations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from trialviz.ctgov.client import CallLog, CTGovClient
from trialviz.ctgov.params import essie_area
from trialviz.schemas.enums import Dimension, InterventionType, OverallStatus, SponsorClass, StudyType, TimeGranularity
from trialviz.schemas.plan import EntityFilter

# Phase buckets follow the registry's display convention: a trial listing both
# PHASE1 and PHASE2 is "Phase 1/Phase 2", not one of each. The expressions make
# the buckets mutually exclusive so the counts add up.
PHASE_BUCKET_EXPRESSIONS: list[tuple[str, str]] = [
    ("Early Phase 1", "AREA[Phase]EARLY_PHASE1"),
    ("Phase 1", "AREA[Phase]PHASE1 AND NOT AREA[Phase]PHASE2"),
    ("Phase 1/Phase 2", "AREA[Phase]PHASE1 AND AREA[Phase]PHASE2"),
    ("Phase 2", "AREA[Phase]PHASE2 AND NOT AREA[Phase]PHASE1 AND NOT AREA[Phase]PHASE3"),
    ("Phase 2/Phase 3", "AREA[Phase]PHASE2 AND AREA[Phase]PHASE3"),
    ("Phase 3", "AREA[Phase]PHASE3 AND NOT AREA[Phase]PHASE2"),
    ("Phase 4", "AREA[Phase]PHASE4"),
    ("Not Applicable", "AREA[Phase]NA"),
]

EXACT_COUNT_DIMENSIONS = {
    Dimension.PHASE,
    Dimension.STATUS,
    Dimension.STUDY_TYPE,
    Dimension.SPONSOR_CLASS,
    Dimension.INTERVENTION_TYPE,
    Dimension.START_YEAR,
    Dimension.COUNTRY,
}


def period_expressions(start_year: int, end_year: int, granularity: TimeGranularity) -> list[tuple[str, str]]:
    """(label, Essie clause) for each time bucket between the two years, inclusive."""
    out: list[tuple[str, str]] = []
    for year in range(start_year, end_year + 1):
        if granularity == TimeGranularity.YEAR:
            out.append((str(year), f"AREA[StartDate]RANGE[{year}-01-01,{year}-12-31]"))
        elif granularity == TimeGranularity.QUARTER:
            for q, (m0, m1, last) in enumerate([(1, 3, 31), (4, 6, 30), (7, 9, 30), (10, 12, 31)], start=1):
                out.append((f"{year}-Q{q}", f"AREA[StartDate]RANGE[{year}-{m0:02d}-01,{year}-{m1:02d}-{last}]"))
        else:
            for month in range(1, 13):
                last = _days_in_month(year, month)
                out.append((f"{year}-{month:02d}", f"AREA[StartDate]RANGE[{year}-{month:02d}-01,{year}-{month:02d}-{last}]"))
    return out


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def bucket_expressions(
    dimension: Dimension,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    granularity: TimeGranularity = TimeGranularity.YEAR,
    candidates: Sequence[str] = (),
) -> list[tuple[str, str]]:
    """Essie clause per category for a dimension, or [] if the dimension is not countable server-side."""
    if dimension == Dimension.PHASE:
        return list(PHASE_BUCKET_EXPRESSIONS)
    if dimension == Dimension.STATUS:
        return [(s.value, essie_area("OverallStatus", s.value)) for s in OverallStatus]
    if dimension == Dimension.STUDY_TYPE:
        return [(s.value, essie_area("StudyType", s.value)) for s in StudyType]
    if dimension == Dimension.SPONSOR_CLASS:
        return [(s.value, essie_area("LeadSponsorClass", s.value)) for s in SponsorClass]
    if dimension == Dimension.INTERVENTION_TYPE:
        return [(t.value, essie_area("InterventionType", t.value)) for t in InterventionType]
    if dimension == Dimension.START_YEAR:
        if start_year is None or end_year is None:
            return []
        return period_expressions(start_year, end_year, granularity)
    if dimension == Dimension.COUNTRY:
        return [(c, essie_area("LocationCountry", c)) for c in candidates]
    return []


async def exact_counts(
    client: CTGovClient,
    f: EntityFilter,
    expressions: Sequence[tuple[str, str]],
    *,
    log: CallLog | None = None,
) -> dict[str, int]:
    """One count request per bucket, run concurrently under the client's concurrency limit."""

    async def one(label: str, expr: str) -> tuple[str, int]:
        return label, await client.count_studies(f, extra_advanced=expr, log=log)

    results = await asyncio.gather(*(one(label, expr) for label, expr in expressions))
    return dict(results)
