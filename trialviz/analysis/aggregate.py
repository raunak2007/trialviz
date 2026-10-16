"""Deterministic group-by over normalized trials, keeping provenance per bucket.

Every bucket remembers which trials put it there and the exact field values
that did so. The chart layer turns those into citations. No value in a bucket
is ever estimated: it is either counted from fetched records or replaced by an
exact count the API reported for that category.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from trialviz.analysis.dimensions import Category, DimensionSpec
from trialviz.analysis.drugs import DrugCanonicalizer
from trialviz.ctgov.records import P_ENROLLMENT, P_START, Trial, encode_value
from trialviz.schemas.enums import Dimension, Measure, TimeGranularity
from trialviz.schemas.response import Citation, Evidence


@dataclass
class Bucket:
    label: str
    code: str | None = None
    value: int = 0  # the measure (trial count or total enrollment)
    trial_count: int = 0  # distinct trials behind the value
    exact: bool = False  # value replaced by an API-side exact count
    period_start: str | None = None  # ISO date for time buckets
    supporting: list[tuple[Trial, list[Evidence]]] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set, repr=False)

    def add(self, trial: Trial, evidence: list[Evidence], measure: Measure) -> bool:
        """Count a trial once per bucket. Returns False when the trial was already counted or has no measure."""
        if trial.nct_id in self._seen:
            return False
        if measure == Measure.TOTAL_ENROLLMENT:
            if trial.enrollment is None:
                return False
            self.value += trial.enrollment
            evidence = [*evidence, Evidence(field=P_ENROLLMENT, value=encode_value(trial.enrollment))]
        else:
            self.value += 1
        self._seen.add(trial.nct_id)
        self.trial_count += 1
        self.supporting.append((trial, evidence))
        return True

    def citations(self, limit: int) -> list[Citation]:
        return [
            Citation(nct_id=t.nct_id, title=t.brief_title, url=t.url, evidence=[t.evidence_title(), *ev])
            for t, ev in self.supporting[:limit]
        ]

    @property
    def trial_ids(self) -> set[str]:
        return {t.nct_id for t, _ in self.supporting}


@dataclass
class GroupResult:
    buckets: list[Bucket]
    uncategorized: int = 0  # trials with no value for the dimension (e.g. missing start date)
    skipped_no_measure: int = 0  # trials dropped because the measure was missing (enrollment)
    categories_before_trim: int = 0
    unclassified_ids: set[str] = field(default_factory=set)  # NCT ids behind the two counts above


# ------------------------------------------------------------------ periods


def period_of(trial: Trial, granularity: TimeGranularity) -> tuple[str, str] | None:
    """(label, ISO period start) of a trial's start date at the requested granularity."""
    year, month = trial.start_year, trial.start_month
    if year is None:
        return None
    month = month or 1
    if granularity == TimeGranularity.YEAR:
        return str(year), f"{year:04d}-01-01"
    if granularity == TimeGranularity.QUARTER:
        q = (month - 1) // 3 + 1
        return f"{year}-Q{q}", f"{year:04d}-{(q - 1) * 3 + 1:02d}-01"
    return f"{year}-{month:02d}", f"{year:04d}-{month:02d}-01"


def all_periods(start_year: int, end_year: int, granularity: TimeGranularity) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for year in range(start_year, end_year + 1):
        if granularity == TimeGranularity.YEAR:
            out.append((str(year), f"{year:04d}-01-01"))
        elif granularity == TimeGranularity.QUARTER:
            out.extend((f"{year}-Q{q}", f"{year:04d}-{(q - 1) * 3 + 1:02d}-01") for q in range(1, 5))
        else:
            out.extend((f"{year}-{m:02d}", f"{year:04d}-{m:02d}-01") for m in range(1, 13))
    return out


# ------------------------------------------------------------------ grouping


def categories_of(
    trial: Trial, spec: DimensionSpec, granularity: TimeGranularity, canon: DrugCanonicalizer | None
) -> list[Category]:
    if spec.dimension == Dimension.START_YEAR:
        period = period_of(trial, granularity)
        if period is None:
            return []
        label, _ = period
        return [Category(label, None, Evidence(field=P_START, value=trial.start_date or ""))]
    cats = spec.extract(trial)
    if spec.dimension == Dimension.INTERVENTION and canon is not None:
        folded: dict[str, Category] = {}
        for c in cats:
            name = canon.canonical(c.label)
            folded.setdefault(name.casefold(), Category(name, c.code, c.evidence))
        cats = list(folded.values())
    return cats


def group_by(
    trials: Sequence[Trial],
    spec: DimensionSpec,
    measure: Measure,
    *,
    granularity: TimeGranularity = TimeGranularity.YEAR,
) -> GroupResult:
    """One bucket per category of `spec`, each trial counted at most once per bucket."""
    canon = DrugCanonicalizer(trials) if spec.dimension == Dimension.INTERVENTION else None
    buckets: dict[str, Bucket] = {}
    uncategorized = 0
    skipped = 0
    unclassified: set[str] = set()
    for trial in trials:
        cats = categories_of(trial, spec, granularity, canon)
        if not cats:
            uncategorized += 1
            unclassified.add(trial.nct_id)
            continue
        if measure == Measure.TOTAL_ENROLLMENT and trial.enrollment is None:
            skipped += 1
            unclassified.add(trial.nct_id)
            continue
        for cat in cats:
            bucket = buckets.get(cat.label)
            if bucket is None:
                period_start = period_of(trial, granularity)[1] if spec.dimension == Dimension.START_YEAR else None
                bucket = buckets[cat.label] = Bucket(label=cat.label, code=cat.code, period_start=period_start)
            bucket.add(trial, [cat.evidence], measure)
    return GroupResult(
        buckets=list(buckets.values()), uncategorized=uncategorized, skipped_no_measure=skipped, unclassified_ids=unclassified
    )


def group_by_pair(
    trials: Sequence[Trial],
    primary: DimensionSpec,
    secondary: DimensionSpec,
    measure: Measure,
    *,
    granularity: TimeGranularity = TimeGranularity.YEAR,
) -> dict[tuple[str, str], Bucket]:
    """Buckets keyed by (primary label, secondary label) for grouped bars and split time trends."""
    canon_primary = DrugCanonicalizer(trials) if primary.dimension == Dimension.INTERVENTION else None
    canon_secondary = DrugCanonicalizer(trials) if secondary.dimension == Dimension.INTERVENTION else None
    buckets: dict[tuple[str, str], Bucket] = {}
    for trial in trials:
        p_cats = categories_of(trial, primary, granularity, canon_primary)
        s_cats = categories_of(trial, secondary, granularity, canon_secondary)
        for p in p_cats:
            for s in s_cats:
                key = (p.label, s.label)
                bucket = buckets.get(key)
                if bucket is None:
                    period_start = period_of(trial, granularity)[1] if primary.dimension == Dimension.START_YEAR else None
                    bucket = buckets[key] = Bucket(label=p.label, code=p.code, period_start=period_start)
                bucket.add(trial, [p.evidence, s.evidence], measure)
    return buckets


# ------------------------------------------------------------------ ordering


def order_buckets(buckets: Iterable[Bucket], spec: DimensionSpec, top_n: int | None) -> tuple[list[Bucket], int]:
    """Apply the dimension's natural order or value ordering, then trim to top_n.

    Returns (ordered buckets, number of categories before trimming).
    """
    items = list(buckets)
    total = len(items)
    if spec.dimension == Dimension.START_YEAR:
        items.sort(key=lambda b: b.period_start or b.label)
        return items, total
    if spec.natural_order is not None:
        items.sort(key=lambda b: (spec.order_index(b.label), b.label))
        return items, total
    items.sort(key=lambda b: (-b.value, b.label))
    limit = top_n if top_n is not None else spec.default_top_n
    if limit is not None:
        items = items[:limit]
    return items, total


def fill_period_gaps(
    buckets: list[Bucket], start_year: int | None, end_year: int | None, granularity: TimeGranularity
) -> list[Bucket]:
    """Insert zero buckets so a time series has one point per period with no gaps."""
    by_label = {b.label: b for b in buckets}
    years = [int(b.period_start[:4]) for b in buckets if b.period_start]
    lo = start_year if start_year is not None else (min(years) if years else None)
    hi = end_year if end_year is not None else (max(years) if years else None)
    if lo is None or hi is None:
        return buckets
    # Never drop data: widen the window if the records fall outside the requested years.
    if years:
        lo, hi = min(lo, min(years)), max(hi, max(years))
    if hi < lo:
        return buckets
    out: list[Bucket] = []
    for label, period_start in all_periods(lo, hi, granularity):
        out.append(by_label.get(label) or Bucket(label=label, period_start=period_start))
    return out
