"""Per-trial numeric fields: histogram binning and scatter rows."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from trialviz.analysis.aggregate import Bucket
from trialviz.ctgov.records import P_COMPLETION, P_ENROLLMENT, P_LOCATIONS, P_START, Trial, encode_value
from trialviz.schemas.enums import Measure, NumericField
from trialviz.schemas.response import Evidence

NUMERIC_TITLES = {
    NumericField.ENROLLMENT: "Enrollment (participants)",
    NumericField.START_YEAR: "Start year",
    NumericField.DURATION_MONTHS: "Duration (months, start to completion)",
    NumericField.SITE_COUNT: "Number of sites",
}

# Fixed, human-readable bin edges. Enrollment and site counts are heavy-tailed, so
# the edges widen geometrically; the last bin is open-ended.
BIN_EDGES: dict[NumericField, list[float]] = {
    NumericField.ENROLLMENT: [0, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10000],
    NumericField.DURATION_MONTHS: [0, 6, 12, 24, 36, 48, 60, 84, 120],
    NumericField.SITE_COUNT: [0, 1, 2, 5, 10, 20, 50, 100, 200, 500],
}


def numeric_value(trial: Trial, f: NumericField) -> tuple[float, list[Evidence]] | None:
    """(value, evidence) for a trial, or None when the field is missing."""
    if f == NumericField.ENROLLMENT:
        if trial.enrollment is None:
            return None
        return float(trial.enrollment), [Evidence(field=P_ENROLLMENT, value=encode_value(trial.enrollment))]
    if f == NumericField.START_YEAR:
        if trial.start_year is None:
            return None
        return float(trial.start_year), [Evidence(field=P_START, value=trial.start_date or "")]
    if f == NumericField.DURATION_MONTHS:
        months = trial.duration_months
        if months is None:
            return None
        return months, [
            Evidence(field=P_START, value=trial.start_date or ""),
            Evidence(field=P_COMPLETION, value=trial.completion_date or ""),
        ]
    if f == NumericField.SITE_COUNT:
        return float(trial.site_count), [Evidence(field=P_LOCATIONS, value=f"{trial.site_count} location entries")]
    raise ValueError(f"unknown numeric field {f}")


@dataclass
class HistogramResult:
    edges: list[float]
    buckets: list[Bucket]
    missing: int = 0


def bin_label(lo: float, hi: float | None, integer: bool) -> str:
    def fmt(x: float) -> str:
        return str(int(x)) if integer else f"{x:g}"

    if hi is None:
        return f"{fmt(lo)}+"
    if integer and hi - lo <= 1:
        return fmt(lo)
    return f"{fmt(lo)} to {fmt(hi - 1) if integer else fmt(hi)}"


def histogram(trials: Sequence[Trial], f: NumericField, *, edges: Sequence[float] | None = None) -> HistogramResult:
    """Bin one numeric field. For start years the bins are the years present in the data."""
    if f == NumericField.START_YEAR and edges is None:
        years = sorted({t.start_year for t in trials if t.start_year is not None})
        edge_list = [float(y) for y in years] + ([float(years[-1]) + 1] if years else [])
    else:
        edge_list = list(edges if edges is not None else BIN_EDGES[f])
    integer = f != NumericField.DURATION_MONTHS
    buckets = [
        Bucket(label=bin_label(edge_list[i], edge_list[i + 1] if i + 1 < len(edge_list) else None, integer), code=None)
        for i in range(len(edge_list))
    ]
    missing = 0
    for trial in trials:
        found = numeric_value(trial, f)
        if found is None:
            missing += 1
            continue
        value, evidence = found
        idx = _bin_index(value, edge_list)
        if idx is None:
            missing += 1
            continue
        buckets[idx].add(trial, evidence, Measure.TRIAL_COUNT)
    return HistogramResult(edges=edge_list, buckets=buckets, missing=missing)


def _bin_index(value: float, edges: Sequence[float]) -> int | None:
    if not edges or value < edges[0] or math.isnan(value):
        return None
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return i
    return len(edges) - 1  # open-ended last bin


@dataclass
class ScatterPoint:
    trial: Trial
    x: float
    y: float
    group: str | None
    evidence: list[Evidence] = field(default_factory=list)


def scatter_points(
    trials: Sequence[Trial],
    x: NumericField,
    y: NumericField,
    group_of: Callable[[Trial], str | None] | None = None,
) -> tuple[list[ScatterPoint], int]:
    """One point per trial with both values present. Returns (points, number skipped)."""
    points: list[ScatterPoint] = []
    skipped = 0
    for trial in trials:
        xv, yv = numeric_value(trial, x), numeric_value(trial, y)
        if xv is None or yv is None:
            skipped += 1
            continue
        points.append(
            ScatterPoint(
                trial=trial,
                x=xv[0],
                y=yv[0],
                group=group_of(trial) if group_of else None,
                evidence=[*xv[1], *yv[1]],
            )
        )
    return points, skipped
