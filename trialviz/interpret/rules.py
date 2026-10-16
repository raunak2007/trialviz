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

from trialviz.schemas.enums import (
    AnalysisType,
    Dimension,
    Measure,
    NetworkKind,
    NumericField,
    OverallStatus,
    Phase,
    TimeGranularity,
)
from trialviz.schemas.plan import EntityFilter, PlanDraft, Series

_RE = {
    "network": re.compile(
        r"\bnetwork\b|co-?occur|\bgraph\b|tested together|given together|appear together|\bcollaborat"
        r"|in the same (?:trials?|stud(?:y|ies))\b|\bcombination (?:studies|trials)\b|\brelationships?\b(?! between)",
        re.I,
    ),
    "comparison": re.compile(r"\bcompare\b|\bcomparison\b|\bvs\.?\b|\bversus\b", re.I),
    "geographic": re.compile(
        r"\bcountr(?:y|ies)\b|\bgeograph|\bnations?\b|\bregions?\b|\bworldwide\b|\bwhere\b(?! .*\bvs\b)", re.I
    ),
    "time": re.compile(
        r"over time|over the years|per (?:year|quarter|month)|each (?:year|quarter|month)|by (?:year|quarter|month)|annual|yearly|quarterly|monthly"
        r"|\btrend|\bchanged\b|\bchange\b|\bgrowth\b|\bevolv|\btimeline\b|\bhistory\b",
        re.I,
    ),
    "range": re.compile(r"\bsince \d{4}\b|\b(?:between|from) \d{4}\b|\b(?:last|past) \d{1,2} years?\b|\bin \d{4}\b", re.I),
    "histogram": re.compile(
        r"\bhistogram\b|distribution of (?:trial )?(?:enrollment|enrolment|sizes?|durations?|sites?)|how (?:big|large)\b"
        r"|\bsizes? of\b",
        re.I,
    ),
    "enrollment_measure": re.compile(
        r"total enrol+ment|how many (?:participants|patients|people)|number of participants|participants (?:are |were )?enrolled"
        r"|enrol+ment totals?|\bparticipants in each\b",
        re.I,
    ),
    "top_n": re.compile(r"\btop\s+(\d{1,3})\b|\b(\d{1,3}) (?:largest|biggest|most common|most active)\b", re.I),
    "scatter": re.compile(r"\bscatter\b|\bagainst\b|\bcorrelat|relationship between", re.I),
    "quarter": re.compile(r"\bquarter|\bquarterly", re.I),
    "month": re.compile(r"\bmonth|\bmonthly", re.I),
}

_DIMENSION_RE: list[tuple[Dimension, re.Pattern[str]]] = [
    (Dimension.INTERVENTION_TYPE, re.compile(r"intervention types?", re.I)),
    (Dimension.SPONSOR_CLASS, re.compile(r"sponsor (?:class|classes|categor(?:y|ies)|types?)|funder types?|industry vs", re.I)),
    (Dimension.STUDY_TYPE, re.compile(r"study types?|interventional (?:vs\.?|versus|or) observational", re.I)),
    (Dimension.STATUS, re.compile(r"\bstatus(?:es)?\b|recruitment status", re.I)),
    (Dimension.COUNTRY, re.compile(r"\bby countr(?:y|ies)\b|\bper country\b|\bacross countries\b", re.I)),
    (
        Dimension.SPONSOR,
        re.compile(
            r"\b(?:which|top(?: \d+)?|most common|leading|biggest|most active|largest) sponsors?\b|\bby sponsor\b"
            r"|\bwho sponsors\b|\bwhich (?:companies|organi[sz]ations)\b",
            re.I,
        ),
    ),
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
_SPONSOR_CUE = r"(?:sponsored|run|conducted|led|funded|registered) by"
# Ordered: the most specific anchors first. Each captures the entity text of a question.
_ENTITY_RES = [
    # "sponsored by X", "run by X", "collaborates with X"
    re.compile(rf"\b(?:{_SPONSOR_CUE}|collaborat\w*\s+with)\s+{_ENT}(?=\s*[?.!,;:]|\s+(?:{_STOP}|on|in)\b|$)", re.I),
    # "together with X", "combined with X", "in combination with X"
    re.compile(
        rf"\b(?:together with|combined with|in combination with|alongside)\s+{_ENT}(?=\s*[?.!,;:]|\s+(?:{_STOP}|in|for)\b|$)",
        re.I,
    ),
    # "trials Novartis started", "studies Pfizer ran": an organization name, capitalized, not a preposition
    re.compile(
        r"\b(?:trials?|studies)\s+(?P<ent>(?!(?:for|in|of|on|with|about|involving|studying|being|that|which)\b)"
        r"[A-Z][A-Za-z0-9&\-'./ ]*?)\s+(?:started|ran|runs?|sponsors|sponsored|conducted|conducts)\b"
    ),
    # "trials for X", "studies involving X" ... up to the end of the clause
    re.compile(rf"\b(?:trials?|studies)\s+{_PREP}\s+{_ENT}(?=\s*[?.!,;:]|\s+(?:{_STOP})\b|$)", re.I),
    # "... for X trials": the last preposition before "trials" wins
    re.compile(rf"^.*\b{_PREP}\s+{_ENT}\s+(?:trials?|studies)\b", re.I),
    # "How are X trials ...", "how many X trials"
    re.compile(rf"\b(?:are|many|of|the|all|do)\s+{_ENT}\s+(?:trials?|studies)\b", re.I),
    # trailing "for X" at the end of the question
    re.compile(rf"\b(?:{_PREP}|as)\s+{_ENT}(?=\s*[?.!,;:]|\s+(?:{_STOP})\b|$)", re.I),
]
# Cleaned entity text that is a grouping word or filler rather than a thing to search for.
_NOT_ENTITY = {
    "phase",
    "phases",
    "year",
    "years",
    "quarter",
    "month",
    "country",
    "countries",
    "sponsor",
    "sponsors",
    "status",
    "statuses",
    "condition",
    "conditions",
    "intervention",
    "interventions",
    "enrollment",
    "participants",
    "same",
    "time",
    "world",
    "terms",
}
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
    "for",
    "of",
    "in",
    "on",
    "involving",
    "about",
}
_TRAILING_FILLER = {
    "trials",
    "studies",
    "trial",
    "study",
    "patients",
    "in",
    "on",
    "for",
    "of",
    "with",
    "by",
    "at",
    "its",
    "their",
}
_DISEASE_WORD = re.compile(
    r"\b(?:cancer|carcinoma|tumou?r|lymphoma|leukemia|sarcoma|melanoma|diabetes|disease|disorder|syndrome|infection|"
    r"failure|hypertension|asthma|copd|obesity|depression|schizophrenia|arthritis|sclerosis|stroke|covid|hiv|hepatitis|"
    r"malaria|dengue|influenza|pneumonia|sepsis|migraine|epilepsy|autism|dementia|alzheimer|parkinson|lupus|gout|"
    r"ebola|cholera|measles|polio|zika|anxiety|insomnia|pain|injury|fracture)\b"
    r"|\w+(?:oma|itis|emia|pathy|osis|iasis|algia|plegia)\b",
    re.I,
)
# Generic-name suffixes (INN stems) that mark a word as a drug, plus a few common names without one.
_DRUG_WORD = re.compile(
    r"\b\w+(?:mab|nib|tide|ciclib|parib|statin|pril|sartan|olol|azole|cillin|mycin|vir|gliptin|flozin|prazole|dipine|"
    r"oxetine|triptan|lukast|afil|caine|oxacin|cycline|zepam|zolam|semide|thiazide|formin|profen|lamide|trexate|platin|"
    r"taxel|rubicin|mustine|fenib|rafenib|tinib|lisib|degib|sertib|zomib|relix|parin|irin|lukast|mide|dronate|"
    r"vastatin|cept|kinra|leukin|feron)\b"
    r"|\b(?:aspirin|insulin|metformin|ibuprofen|paracetamol|acetaminophen|morphine|lithium|ketamine|psilocybin|"
    r"semaglutide|tirzepatide|dexamethasone|hydroxychloroquine|remdesivir|placebo)\b",
    re.I,
)
# Organizations a question is likely to name as a sponsor.
_SPONSOR_WORD = re.compile(
    r"\b(?:pfizer|moderna|novartis|roche|genentech|merck|msd|astrazeneca|sanofi|gsk|glaxosmithkline|bayer|abbvie|amgen|"
    r"gilead|lilly|bristol|bms|squibb|johnson|janssen|takeda|boehringer|regeneron|biogen|novo nordisk|astellas|daiichi|"
    r"eisai|vertex|biontech|national cancer institute|nci|nih|national institutes? of health|mayo clinic|md anderson|"
    r"university|hospital|institute|foundation|ministry|pharma\w*|therapeutics|biosciences|inc\.?|ltd\.?|llc|gmbh|ag)\b",
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
        pos = 0
        while (m := pattern.search(text, pos)) is not None:
            pos = m.start() + 1  # allow overlapping candidates: "in the same trials as X" then "as X"
            cleaned = _clean_entity(m.group("ent"))
            if cleaned and cleaned.lower() not in _NOT_ENTITY and cleaned.split()[0].lower() not in _NOT_ENTITY:
                return cleaned
    return ""


_AND_LIST_RE = re.compile(
    rf"\b(?:across|between|among|for|of|in)\s+(?P<list>[A-Za-z][A-Za-z0-9\-'/ ]*?(?:,\s*[A-Za-z][A-Za-z0-9\-'/ ]*?)*,?\s+and\s+"
    rf"[A-Za-z][A-Za-z0-9\-'/ ]*?)(?=\s+(?:trials?|studies)\b|\s*[?.!;:]|\s+(?:{_STOP})\b|$)",
    re.I,
)


def _comparison_sides(text: str) -> list[str]:
    """Entity names on each side of 'vs' / 'versus', or the items of an 'A, B, and C' list."""
    m = _VS_RE.search(text)
    if m:
        left = _clean_entity(_after_last_preposition(m.group("a")))
        right = _clean_entity(m.group("b"))
        return [s for s in (left, right) if s]
    m = _AND_LIST_RE.search(text)
    if not m:
        return []
    raw = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", m.group("list"))
    raw[0] = _after_last_preposition(raw[0])
    parts = [_clean_entity(p) for p in raw]
    return [p for p in parts if p and p.lower() not in _NOT_ENTITY]


def _entity_filter(entity: str, text: str) -> EntityFilter:
    """Classify an extracted entity as drug, condition, or free text.

    Only adjacency counts as evidence ("the drug X", "patients with X"); a role word elsewhere in
    the question ("which drugs ... for melanoma") says nothing about the entity itself.
    """
    if not entity:
        return EntityFilter()
    if entity.lower().endswith("-sponsored"):
        return EntityFilter(sponsors=[entity[: -len("-sponsored")]])
    ent = re.escape(entity)
    if re.search(rf"\b(?:{_SPONSOR_CUE}|collaborat\w*\s+with)\s+(?:the\s+)?{ent}\b", text, re.I):
        return EntityFilter(sponsors=[entity])
    if re.search(
        rf"\b(?:drugs?|medications?|treatments?|compounds?|interventions?|together with|combined with|in combination with)"
        rf"\s+(?:called\s+|named\s+)?{ent}\b",
        text,
        re.I,
    ):
        return EntityFilter(drugs=[entity])
    if re.search(
        rf"\b(?:conditions?|diseases?|patients with|indications?)\s+(?:called\s+|named\s+)?{ent}\b", text, re.I
    ) or _DISEASE_WORD.search(entity):
        return EntityFilter(conditions=[entity])
    if _DRUG_WORD.search(entity):
        return EntityFilter(drugs=[entity])
    if re.search(rf"\b{ent}\s+(?:started|ran|runs?|sponsors|sponsored|conducted|conducts)\b", text, re.I):
        return EntityFilter(sponsors=[entity])
    if _SPONSOR_WORD.search(entity):
        return EntityFilter(sponsors=[entity])
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
    measure = Measure.TOTAL_ENROLLMENT if _RE["enrollment_measure"].search(text) else Measure.TRIAL_COUNT
    top_match = _RE["top_n"].search(text)
    top_n = int(top_match.group(1) or top_match.group(2)) if top_match else None

    if _RE["network"].search(text):
        kind = _network_kind(text)
        draft = PlanDraft(
            analysis=AnalysisType.NETWORK,
            network=kind,
            filter=base.merged_over(_entity_filter(entity, text)),
            interpretation=f"Build a {_NETWORK_WORDS[kind]} network.",
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
            measure=measure,
            top_n=top_n,
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
            top_n=top_n,
            interpretation="Count trials per country.",
            assumptions=assumptions,
        )

    # Explicit trend wording wins; a bare year range with a grouping word is a filtered distribution;
    # a bare year range with nothing else is counted per period.
    if _RE["time"].search(text) or (_RE["range"].search(text) and dimension is None):
        return PlanDraft(
            analysis=AnalysisType.TIME_TREND,
            split_by=dimension if dimension != Dimension.START_YEAR else None,
            measure=measure,
            time_granularity=granularity,
            filter=base.merged_over(_entity_filter(entity, text)),
            interpretation=f"Count trials started per {granularity.value}.",
            assumptions=assumptions,
        )

    if dimension is not None:
        return PlanDraft(
            analysis=AnalysisType.DISTRIBUTION,
            dimension=dimension,
            measure=measure,
            top_n=top_n,
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


_NETWORK_WORDS = {
    NetworkKind.SPONSOR_DRUG: "sponsor to drug",
    NetworkKind.DRUG_DRUG: "drug combination (same arm)",
    NetworkKind.DRUG_DRUG_SAME_TRIAL: "drug to drug (same trial)",
    NetworkKind.CONDITION_DRUG: "condition to drug",
    NetworkKind.SPONSOR_CONDITION: "sponsor to condition",
    NetworkKind.SPONSOR_COLLABORATOR: "sponsor to collaborator",
}


def _network_kind(text: str) -> NetworkKind:
    sponsor = re.search(r"\bsponsors?\b", text, re.I)
    drug = re.search(r"\bdrugs?\b|\binterventions?\b|\btreatments?\b", text, re.I)
    condition = re.search(r"\bconditions?\b|\bdiseases?\b", text, re.I)
    collab = re.search(r"\bcollaborat", text, re.I)
    if collab:
        return NetworkKind.SPONSOR_COLLABORATOR
    if sponsor and drug:
        return NetworkKind.SPONSOR_DRUG
    if sponsor and condition:
        return NetworkKind.SPONSOR_CONDITION
    if condition and drug:
        return NetworkKind.CONDITION_DRUG
    if re.search(r"\bcombination|\bcombined\b|\btogether\b|\bco-?administer", text, re.I):
        return NetworkKind.DRUG_DRUG
    if re.search(r"co-?occur|same (?:trial|study|studies|trials)|drug\s*(?:to|-|↔|and)\s*drug", text, re.I):
        return NetworkKind.DRUG_DRUG_SAME_TRIAL
    return NetworkKind.SPONSOR_DRUG
