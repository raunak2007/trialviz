"""Deterministic, keyword-driven planner.

Used when no model is configured, when the model is unavailable, or when the
request asks for it explicitly. It is intentionally conservative: when it
cannot tell what to group by it returns `unsupported` so the API asks for
clarification instead of guessing.

Structured request fields are the primary source of entities; the regexes
below only pick up entities from the question when those fields are empty.
"""

from __future__ import annotations

import re
from datetime import date

from trialviz.schemas.enums import AnalysisType, Dimension, NetworkKind, NumericField, OverallStatus, Phase, TimeGranularity
from trialviz.schemas.plan import EntityFilter, PlanDraft, Series

_RE = {
    "network": re.compile(
        r"\bnetwork\b|co-?occur|\bgraph\b|tested together|\bcombination (?:studies|trials)\b|\brelationships?\b", re.I
    ),
    "comparison": re.compile(r"\bcompare\b|\bcomparison\b|\bvs\.?\b|\bversus\b", re.I),
    "geographic": re.compile(r"\bcountr(?:y|ies)\b|\bgeograph|\bnations?\b|\bregions?\b|\bworldwide\b", re.I),
    "time": re.compile(
        r"over time|over the years|per (?:year|quarter|month)|each (?:year|quarter|month)|by (?:year|quarter|month)|annual|yearly|quarterly|monthly"
        r"|\btrend|\bchanged\b|\bchange\b|\bgrowth\b|\bevolv|\btimeline\b|\bhistory\b",
        re.I,
    ),
    "range": re.compile(r"\bsince \d{4}\b|\b(?:between|from) \d{4}\b|\b(?:last|past) \d{1,2} years?\b|\bin \d{4}\b", re.I),
    "histogram": re.compile(
        r"\bhistogram\b|distribution of (?:enrollment|enrolment|trial sizes?|durations?|sites?)|how (?:big|large)\b|\bsizes? of\b",
        re.I,
    ),
    "scatter": re.compile(r"\bscatter\b|\bagainst\b|\bcorrelat|relationship between", re.I),
    "quarter": re.compile(r"\bquarter|\bquarterly", re.I),
    "month": re.compile(r"\bmonth|\bmonthly", re.I),
}

_DIMENSION_RE: list[tuple[Dimension, re.Pattern[str]]] = [
    (Dimension.INTERVENTION_TYPE, re.compile(r"intervention types?", re.I)),
    (Dimension.SPONSOR_CLASS, re.compile(r"sponsor (?:class|classes|categor(?:y|ies)|types?)|funder types?|industry vs", re.I)),
    (Dimension.STUDY_TYPE, re.compile(r"study types?|interventional vs observational", re.I)),
    (Dimension.STATUS, re.compile(r"\bstatus(?:es)?\b|recruitment status", re.I)),
    (Dimension.SPONSOR, re.compile(r"\b(?:which|top|most common|leading|biggest|most active) sponsors?\b|\bby sponsor\b", re.I)),
    (Dimension.CONDITION, re.compile(r"\b(?:which|top|most common) conditions?\b|\bby condition\b|\bindications\b", re.I)),
    (
        Dimension.INTERVENTION,
        re.compile(r"\b(?:which|top|most common|most studied|most tested) (?:drugs?|interventions?|treatments?)\b", re.I),
    ),
    # "phase 3" is a filter, not a grouping; only grouping phrasings count here
    (
        Dimension.PHASE,
        re.compile(r"\bphases\b|\bby phase\b|\bper phase\b|\bacross phase|\bphase distribution\b|\beach phase\b", re.I),
    ),
]

_NUMERIC_RE: list[tuple[NumericField, re.Pattern[str]]] = [
    (NumericField.ENROLLMENT, re.compile(r"enrol+ment|participants|sample size|trial sizes?", re.I)),
    (NumericField.DURATION_MONTHS, re.compile(r"\bduration|how long", re.I)),
    (NumericField.SITE_COUNT, re.compile(r"\bsites?\b|\blocations?\b|\bcenters?\b|\bcentres?\b", re.I)),
    (NumericField.START_YEAR, re.compile(r"start (?:year|date)|\byear\b", re.I)),
]

_STATUS_RE: list[tuple[OverallStatus, re.Pattern[str]]] = [
    (OverallStatus.RECRUITING, re.compile(r"\brecruiting\b(?! status)", re.I)),
    (OverallStatus.COMPLETED, re.compile(r"\bcompleted\b", re.I)),
    (OverallStatus.ACTIVE_NOT_RECRUITING, re.compile(r"\bactive\b", re.I)),
    (OverallStatus.TERMINATED, re.compile(r"\bterminated\b", re.I)),
    (OverallStatus.WITHDRAWN, re.compile(r"\bwithdrawn\b", re.I)),
    (OverallStatus.NOT_YET_RECRUITING, re.compile(r"not yet recruiting", re.I)),
]

_PHASE_FILTER_RE = re.compile(r"\bphase\s*(?:(1|2|3|4)|(i{1,3}|iv))\b", re.I)
_YEAR_RANGE_RES = [
    re.compile(r"\b(?:between|from)\s+(\d{4})\s+(?:and|to)\s+(\d{4})\b", re.I),
    re.compile(r"\b(\d{4})\s*(?:-|–|to)\s*(\d{4})\b"),
]
_SINCE_RE = re.compile(r"\b(?:since|from|after)\s+(\d{4})\b", re.I)
_LAST_N_RE = re.compile(r"\b(?:last|past)\s+(\d{1,2})\s+years?\b", re.I)
_IN_YEAR_RE = re.compile(r"\bin\s+(\d{4})\b", re.I)

_PREP = r"(?:for|involving|of|on|about|in|with|studying|testing|regarding)"
_STOP = (
    r"since|per|by|across|over|between|from|each|vs\.?|versus|and|that|which|where|start|started|changed|"
    r"distributed|grouped|compared|during|within|in \d{4}|last|past"
)
_ENT = r"(?P<ent>[A-Za-z][A-Za-z0-9\-'/ ]*?)"
# Ordered: the most specific anchors first. Each captures the entity text of a question.
_ENTITY_RES = [
    # "trials for X", "studies involving X" ... up to the end of the clause
    re.compile(rf"\b(?:trials?|studies)\s+{_PREP}\s+{_ENT}(?=\s*[?.!,;:]|\s+(?:{_STOP})\b|$)", re.I),
    # "... for X trials": the last preposition before "trials" wins
    re.compile(rf"^.*\b{_PREP}\s+{_ENT}\s+(?:trials?|studies)\b", re.I),
    # "How are X trials ...", "how many X trials"
    re.compile(rf"\b(?:are|many|of|the|all|do)\s+{_ENT}\s+(?:trials?|studies)\b", re.I),
    # trailing "for X" at the end of the question
    re.compile(rf"\b{_PREP}\s+{_ENT}(?=\s*[?.!,;:]|\s+(?:{_STOP})\b|$)", re.I),
]
_VS_RE = re.compile(
    rf"(?P<a>[A-Za-z][A-Za-z0-9\-'/ :]*?)\s+(?:vs\.?|versus)\s+(?P<b>[A-Za-z][A-Za-z0-9\-'/ ]*?)(?=\s*[?.!,;]|\s+(?:{_STOP})\b|$)",
    re.I,
)
_LEADING_FILLER = {
    "the",
    "this",
    "that",
    "these",
    "those",
    "each",
    "all",
    "a",
    "an",
    "drug",
    "drugs",
    "condition",
    "conditions",
    "disease",
    "diseases",
    "trials",
    "studies",
    "trial",
    "study",
    "combination",
    "recruiting",
    "completed",
    "active",
    "terminated",
    "withdrawn",
    "interventional",
    "observational",
    "two",
    "three",
    "both",
    "patients",
    "with",
    "called",
    "named",
}
_TRAILING_FILLER = {"trials", "studies", "trial", "study", "patients"}
_DISEASE_WORD = re.compile(
    r"\b(?:cancer|carcinoma|tumou?r|lymphoma|leukemia|sarcoma|melanoma|diabetes|disease|disorder|syndrome|infection|"
    r"failure|hypertension|asthma|copd|obesity|depression|schizophrenia|arthritis|sclerosis|stroke|covid|hiv|hepatitis)\b"
    r"|\w+(?:oma|itis|emia|pathy|osis)\b",
    re.I,
)


def _match_dimension(text: str) -> Dimension | None:
    for dim, pattern in _DIMENSION_RE:
        if pattern.search(text):
            return dim
    return None


def _numeric_fields(text: str) -> list[NumericField]:
    found = [f for f, pattern in _NUMERIC_RE if pattern.search(text)]
    return list(dict.fromkeys(found))


def _years(text: str, today: date) -> tuple[int | None, int | None]:
    for pattern in _YEAR_RANGE_RES:
        m = pattern.search(text)
        if m:
            return int(m.group(1)), int(m.group(2))
    if m := _SINCE_RE.search(text):
        return int(m.group(1)), None
    if m := _LAST_N_RE.search(text):
        return today.year - int(m.group(1)), today.year
    if m := _IN_YEAR_RE.search(text):
        year = int(m.group(1))
        return year, year
    return None, None


def _phase_filter(text: str) -> list[Phase]:
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
    out: list[Phase] = []
    for m in _PHASE_FILTER_RE.finditer(text):
        number = m.group(1) or roman.get(m.group(2).lower(), "")
        if number:
            out.append(Phase(f"PHASE{number}"))
    return list(dict.fromkeys(out))


def _clean_entity(raw: str) -> str:
    """Strip filler and modifier words that are not part of the entity name."""
    text = raw.split(":")[-1]
    text = _PHASE_FILTER_RE.sub(" ", text)
    text = " ".join(text.split()).strip(" ,./")
    words = text.split()
    while words and words[0].lower() in _LEADING_FILLER:
        words = words[1:]
    while words and words[-1].lower() in _TRAILING_FILLER:
        words = words[:-1]
    return " ".join(words)


def _after_last_preposition(text: str) -> str:
    """'trials involving nivolumab' -> 'nivolumab'; 'two conditions: asthma' -> 'asthma'."""
    m = re.search(rf"^.*\b{_PREP}\s+(?P<rest>.+)$", text, re.I)
    return m.group("rest") if m else text


def _entity_text(text: str) -> str:
    for pattern in _ENTITY_RES:
        m = pattern.search(text)
        if m:
            cleaned = _clean_entity(m.group("ent"))
            if cleaned:
                return cleaned
    return ""


def _comparison_sides(text: str) -> list[str]:
    """Entity names on each side of 'vs' / 'versus', cleaned of leading clause words."""
    m = _VS_RE.search(text)
    if not m:
        return []
    left = _clean_entity(_after_last_preposition(m.group("a")))
    right = _clean_entity(m.group("b"))
    return [s for s in (left, right) if s]


def _entity_filter(entity: str, text: str) -> EntityFilter:
    """Classify an extracted entity as drug, condition, or free text.

    Only adjacency counts as evidence ("the drug X", "patients with X"); a role word elsewhere in
    the question ("which drugs ... for melanoma") says nothing about the entity itself.
    """
    if not entity:
        return EntityFilter()
    ent = re.escape(entity)
    if re.search(
        rf"\b(?:drugs?|medications?|treatments?|compounds?|interventions?)\s+(?:called\s+|named\s+)?{ent}\b", text, re.I
    ):
        return EntityFilter(drugs=[entity])
    if re.search(
        rf"\b(?:conditions?|diseases?|patients with|indications?)\s+(?:called\s+|named\s+)?{ent}\b", text, re.I
    ) or _DISEASE_WORD.search(entity):
        return EntityFilter(conditions=[entity])
    return EntityFilter(terms=[entity])


def rule_plan(query: str, structured: EntityFilter, *, today: date | None = None) -> PlanDraft:
    today = today or date.today()
    text = query.strip()
    assumptions = ["Planned with the rule-based parser (no language model)."]

    start_year, end_year = _years(text, today)
    statuses = [s for s, pattern in _STATUS_RE if pattern.search(text)]
    phases = _phase_filter(text) if not re.search(r"across phases|by phase|distributed across", text, re.I) else []
    base = EntityFilter(statuses=statuses, phases=phases, start_year=start_year, end_year=end_year)

    entity = "" if structured.has_entity() else _entity_text(text)
    granularity = (
        TimeGranularity.QUARTER
        if _RE["quarter"].search(text)
        else TimeGranularity.MONTH
        if _RE["month"].search(text)
        else TimeGranularity.YEAR
    )
    dimension = _match_dimension(text)
    numeric = _numeric_fields(text)

    if _RE["network"].search(text):
        kind = _network_kind(text)
        draft = PlanDraft(
            analysis=AnalysisType.NETWORK,
            network=kind,
            filter=base.merged_over(_entity_filter(entity, text)),
            interpretation=f"Build a {kind.value.replace('_', ' to ')} network.",
            assumptions=assumptions,
        )
        return draft

    if _RE["scatter"].search(text) and len(numeric) >= 2:
        return PlanDraft(
            analysis=AnalysisType.SCATTER,
            numeric_x=numeric[1],
            numeric_y=numeric[0],
            split_by=dimension,
            filter=base.merged_over(_entity_filter(entity, text)),
            interpretation=f"Scatter {numeric[0].value} against {numeric[1].value}, one point per trial.",
            assumptions=assumptions,
        )

    if _RE["comparison"].search(text):
        parts = _comparison_sides(text)
        series = [Series(label=p, filter=_entity_filter(p, text)) for p in parts]
        return PlanDraft(
            analysis=AnalysisType.COMPARISON,
            dimension=dimension
            if dimension not in (None, Dimension.START_YEAR)
            else (Dimension.START_YEAR if _RE["time"].search(text) else Dimension.PHASE),
            series=series,
            filter=base,
            time_granularity=granularity,
            interpretation=f"Compare {' vs '.join(parts) if parts else 'the requested entities'} by {(dimension or Dimension.PHASE).value}.",
            assumptions=assumptions,
        )

    if _RE["histogram"].search(text):
        return PlanDraft(
            analysis=AnalysisType.HISTOGRAM,
            numeric_x=numeric[0] if numeric else NumericField.ENROLLMENT,
            filter=base.merged_over(_entity_filter(entity, text)),
            interpretation="Histogram of a per-trial numeric field.",
            assumptions=assumptions,
        )

    if _RE["geographic"].search(text):
        return PlanDraft(
            analysis=AnalysisType.GEOGRAPHIC,
            filter=base.merged_over(_entity_filter(entity, text)),
            interpretation="Count trials per country.",
            assumptions=assumptions,
        )

    # Explicit trend wording wins; a bare year range with a grouping word is a filtered distribution;
    # a bare year range with nothing else is counted per period.
    if _RE["time"].search(text) or (_RE["range"].search(text) and dimension is None):
        return PlanDraft(
            analysis=AnalysisType.TIME_TREND,
            split_by=dimension if dimension != Dimension.START_YEAR else None,
            time_granularity=granularity,
            filter=base.merged_over(_entity_filter(entity, text)),
            interpretation=f"Count trials started per {granularity.value}.",
            assumptions=assumptions,
        )

    if dimension is not None:
        return PlanDraft(
            analysis=AnalysisType.DISTRIBUTION,
            dimension=dimension,
            filter=base.merged_over(_entity_filter(entity, text)),
            interpretation=f"Count trials by {dimension.value}.",
            assumptions=assumptions,
        )

    return PlanDraft(
        analysis=AnalysisType.UNSUPPORTED,
        interpretation="The rule-based parser could not identify what to count or group by.",
        unsupported_reason="Could not tell what to visualize. Say what to group by (phase, year, country, sponsor, "
        "intervention type) or ask for a network, comparison, or histogram.",
        assumptions=assumptions,
    )


def _network_kind(text: str) -> NetworkKind:
    sponsor = re.search(r"\bsponsors?\b", text, re.I)
    drug = re.search(r"\bdrugs?\b|\binterventions?\b|\btreatments?\b", text, re.I)
    condition = re.search(r"\bconditions?\b|\bdiseases?\b", text, re.I)
    collab = re.search(r"\bcollaborat", text, re.I)
    if sponsor and collab:
        return NetworkKind.SPONSOR_COLLABORATOR
    if sponsor and drug:
        return NetworkKind.SPONSOR_DRUG
    if sponsor and condition:
        return NetworkKind.SPONSOR_CONDITION
    if condition and drug:
        return NetworkKind.CONDITION_DRUG
    if re.search(r"co-?occur|combination|together|drug\s*(?:to|-|↔|and)\s*drug", text, re.I):
        return NetworkKind.DRUG_DRUG
    return NetworkKind.SPONSOR_DRUG
