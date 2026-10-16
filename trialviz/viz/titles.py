"""Deterministic titles and subtitles built from the plan, never from the model."""

from __future__ import annotations

from trialviz.analysis.dimensions import STATUS_LABELS, STUDY_TYPE_LABELS, spec_for
from trialviz.analysis.numeric import NUMERIC_TITLES
from trialviz.ctgov.records import PHASE_LABELS
from trialviz.schemas.enums import AnalysisType, Dimension, Measure, NetworkKind, TimeGranularity
from trialviz.schemas.plan import EntityFilter, QueryPlan


def _join(items: list[str], sep: str = " or ") -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {sep.strip()} " + items[-1]


def entity_phrase(f: EntityFilter) -> str:
    """'pembrolizumab trials in melanoma', 'melanoma trials', 'trials sponsored by Novartis'."""
    parts: list[str] = []
    subject = ""
    if f.drugs:
        subject = _join(f.drugs)
    elif f.conditions:
        subject = _join(f.conditions)
    elif f.terms:
        subject = _join(f.terms)
    phrase = f"{subject} trials" if subject else "trials"
    if f.drugs and f.conditions:
        parts.append(f"in {_join(f.conditions)}")
    if f.terms and (f.drugs or f.conditions):
        parts.append(f"matching {_join(f.terms)}")
    if f.sponsors:
        parts.append(f"sponsored by {_join(f.sponsors)}")
    if f.countries:
        parts.append(f"with sites in {_join(f.countries)}")
    return " ".join([phrase, *parts])


def filter_caveats(f: EntityFilter) -> list[str]:
    """Human-readable constraints that belong in a subtitle."""
    out: list[str] = []
    if f.phases:
        out.append(_join([PHASE_LABELS.get(p.value, p.value) for p in f.phases]))
    if f.statuses:
        out.append(_join([STATUS_LABELS.get(s.value, s.value) for s in f.statuses]).lower())
    if f.study_types:
        out.append(_join([STUDY_TYPE_LABELS.get(s.value, s.value) for s in f.study_types]).lower() + " studies")
    if f.start_year is not None and f.end_year is not None:
        out.append(f"started {f.start_year} to {f.end_year}")
    elif f.start_year is not None:
        out.append(f"started {f.start_year} or later")
    elif f.end_year is not None:
        out.append(f"started {f.end_year} or earlier")
    return out


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def measure_phrase(measure: Measure) -> str:
    return "total enrollment" if measure == Measure.TOTAL_ENROLLMENT else "number of trials"


GRANULARITY_WORD = {TimeGranularity.YEAR: "year", TimeGranularity.QUARTER: "quarter", TimeGranularity.MONTH: "month"}

NETWORK_TITLES: dict[NetworkKind, str] = {
    NetworkKind.SPONSOR_DRUG: "Sponsor to drug network for {entity}",
    NetworkKind.DRUG_DRUG: "Drugs tested together in {entity}",
    NetworkKind.CONDITION_DRUG: "Condition to drug network for {entity}",
    NetworkKind.SPONSOR_CONDITION: "Sponsor to condition network for {entity}",
    NetworkKind.SPONSOR_COLLABORATOR: "Lead sponsor to collaborator network for {entity}",
}


def title_for(plan: QueryPlan) -> str:
    f = plan.filter
    entity = entity_phrase(f)
    a = plan.analysis
    if a == AnalysisType.COMPARISON:
        labels = _join([s.label for s in plan.series], sep=" vs ")
        dim = spec_for(plan.dimension).title.lower() if plan.dimension else "category"
        measure = "Total enrollment" if plan.measure == Measure.TOTAL_ENROLLMENT else "Trials"
        if plan.dimension == Dimension.START_YEAR:
            return f"{measure} started per {GRANULARITY_WORD[plan.time_granularity]}: {labels}"
        return f"{measure} by {dim}: {labels}"
    if a == AnalysisType.TIME_TREND:
        measure = "Total enrollment in" if plan.measure == Measure.TOTAL_ENROLLMENT else ""
        base = f"{measure} {entity}".strip()
        title = f"{_cap(base)} started per {GRANULARITY_WORD[plan.time_granularity]}"
        if plan.split_by:
            title += f", by {spec_for(plan.split_by).title.lower()}"
        if f.start_year is not None and f.end_year is not None:
            title += f", {f.start_year} to {f.end_year}"
        elif f.start_year is not None:
            title += f" since {f.start_year}"
        return title
    if a == AnalysisType.GEOGRAPHIC:
        status = ""
        if f.statuses and len(f.statuses) == 1:
            status = STATUS_LABELS.get(f.statuses[0].value, "").lower() + " "
        return _cap(f"countries with the most {status}{entity}")
    if a == AnalysisType.DISTRIBUTION:
        dim = spec_for(plan.dimension).title.lower() if plan.dimension else "category"
        if plan.measure == Measure.TOTAL_ENROLLMENT:
            return _cap(f"total enrollment in {entity} by {dim}")
        title = _cap(f"{entity} by {dim}")
        if plan.split_by:
            title += f" and {spec_for(plan.split_by).title.lower()}"
        return title
    if a == AnalysisType.NETWORK and plan.network:
        return NETWORK_TITLES[plan.network].format(entity=entity)
    if a == AnalysisType.SCATTER and plan.numeric_x and plan.numeric_y:
        return f"{NUMERIC_TITLES[plan.numeric_y]} vs {NUMERIC_TITLES[plan.numeric_x].lower()} for {entity}"
    if a == AnalysisType.HISTOGRAM and plan.numeric_x:
        return _cap(f"distribution of {NUMERIC_TITLES[plan.numeric_x].lower()} across {entity}")
    return _cap(entity)


def subtitle_for(plan: QueryPlan, extra: list[str]) -> str | None:
    parts = filter_caveats(plan.filter) + [e for e in extra if e]
    return " · ".join(_cap(p) for p in parts) if parts else None
