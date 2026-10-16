"""How each categorical dimension is read off a Trial, with citation evidence.

A `DimensionSpec` is the single place that knows a dimension's row key, axis
title, display labels, natural ordering, and the JSON path that proves a
trial belongs to a category. Aggregation, exact counting, and chart assembly
all go through it, so adding a dimension is one entry here plus (optionally)
one Essie expression in `ctgov.exact_counts`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from trialviz.ctgov.records import (
    P_CONDITIONS,
    P_INTERVENTIONS,
    P_LEAD_SPONSOR,
    P_LEAD_SPONSOR_CLASS,
    P_LOCATIONS,
    P_PHASES,
    P_START,
    P_STATUS,
    P_STUDY_TYPE,
    PHASE_LABEL_ORDER,
    Trial,
    encode_value,
)
from trialviz.schemas.enums import Dimension, InterventionType, OverallStatus, SponsorClass, StudyType
from trialviz.schemas.response import Evidence

STATUS_LABELS = {
    "NOT_YET_RECRUITING": "Not yet recruiting",
    "RECRUITING": "Recruiting",
    "ENROLLING_BY_INVITATION": "Enrolling by invitation",
    "ACTIVE_NOT_RECRUITING": "Active, not recruiting",
    "COMPLETED": "Completed",
    "SUSPENDED": "Suspended",
    "TERMINATED": "Terminated",
    "WITHDRAWN": "Withdrawn",
    "AVAILABLE": "Available",
    "NO_LONGER_AVAILABLE": "No longer available",
    "TEMPORARILY_NOT_AVAILABLE": "Temporarily not available",
    "APPROVED_FOR_MARKETING": "Approved for marketing",
    "WITHHELD": "Withheld",
    "UNKNOWN": "Unknown status",
}

SPONSOR_CLASS_LABELS = {
    "NIH": "NIH",
    "FED": "U.S. federal agency",
    "OTHER_GOV": "Other government",
    "INDIV": "Individual",
    "INDUSTRY": "Industry",
    "NETWORK": "Network",
    "AMBIG": "Ambiguous",
    "OTHER": "Other (academic, hospital, nonprofit)",
    "UNKNOWN": "Unknown",
}

INTERVENTION_TYPE_LABELS = {
    "DRUG": "Drug",
    "BIOLOGICAL": "Biological",
    "DEVICE": "Device",
    "PROCEDURE": "Procedure",
    "RADIATION": "Radiation",
    "BEHAVIORAL": "Behavioral",
    "GENETIC": "Genetic",
    "DIETARY_SUPPLEMENT": "Dietary supplement",
    "COMBINATION_PRODUCT": "Combination product",
    "DIAGNOSTIC_TEST": "Diagnostic test",
    "OTHER": "Other",
}

STUDY_TYPE_LABELS = {
    "INTERVENTIONAL": "Interventional",
    "OBSERVATIONAL": "Observational",
    "EXPANDED_ACCESS": "Expanded access",
}

MISSING = "Not specified"


@dataclass(frozen=True)
class Category:
    label: str  # display value, used as the row's dimension value
    code: str | None  # raw API value when one exists
    evidence: Evidence


@dataclass(frozen=True)
class DimensionSpec:
    dimension: Dimension
    key: str  # row key holding the display label
    title: str  # axis title
    multi_valued: bool  # one trial may fall into several categories
    natural_order: Sequence[str] | None  # fixed category order; None means sort by value
    default_top_n: int | None  # trim long tails by default
    extract: Callable[[Trial], list[Category]]

    @property
    def code_key(self) -> str:
        return f"{self.key}_code"

    def order_index(self, label: str) -> int:
        if self.natural_order is None:
            return 0
        try:
            return list(self.natural_order).index(label)
        except ValueError:
            return len(self.natural_order)


def _enum_label(table: dict[str, str], code: str | None) -> str:
    if not code:
        return MISSING
    return table.get(code, code.replace("_", " ").title())


def _phase(t: Trial) -> list[Category]:
    return [Category(t.phase_label, "|".join(t.phases) or None, Evidence(field=P_PHASES, value=encode_value(list(t.phases))))]


def _status(t: Trial) -> list[Category]:
    return [
        Category(
            _enum_label(STATUS_LABELS, t.overall_status), t.overall_status, Evidence(field=P_STATUS, value=t.overall_status or "")
        )
    ]


def _study_type(t: Trial) -> list[Category]:
    return [
        Category(
            _enum_label(STUDY_TYPE_LABELS, t.study_type), t.study_type, Evidence(field=P_STUDY_TYPE, value=t.study_type or "")
        )
    ]


def _sponsor_class(t: Trial) -> list[Category]:
    code = t.lead_sponsor_class
    return [Category(_enum_label(SPONSOR_CLASS_LABELS, code), code, Evidence(field=P_LEAD_SPONSOR_CLASS, value=code or ""))]


def _sponsor(t: Trial) -> list[Category]:
    name = t.lead_sponsor or MISSING
    return [Category(name, None, Evidence(field=P_LEAD_SPONSOR, value=t.lead_sponsor or ""))]


def _country(t: Trial) -> list[Category]:
    return [
        Category(c, None, Evidence(field=f"{P_LOCATIONS}[{t.country_location_index[c]}].country", value=c)) for c in t.countries
    ]


def _intervention_type(t: Trial) -> list[Category]:
    seen: dict[str, Category] = {}
    for i in t.interventions:
        code = i.type or "OTHER"
        if code not in seen:
            seen[code] = Category(
                _enum_label(INTERVENTION_TYPE_LABELS, code),
                code,
                Evidence(field=f"{P_INTERVENTIONS}[{i.index}].type", value=i.type or ""),
            )
    return list(seen.values())


def _condition(t: Trial) -> list[Category]:
    out: list[Category] = []
    seen: set[str] = set()
    for idx, c in enumerate(t.conditions):
        key = c.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(Category(c, None, Evidence(field=f"{P_CONDITIONS}[{idx}]", value=c)))
    return out


def _intervention(t: Trial) -> list[Category]:
    # Raw names; the network module applies MeSH-based alias folding when it builds graphs,
    # and the aggregator applies the same folding through `canonical_drug_categories`.
    out: list[Category] = []
    seen: set[str] = set()
    for i in t.drug_interventions():
        key = i.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(Category(i.name, None, Evidence(field=f"{P_INTERVENTIONS}[{i.index}].name", value=i.name)))
    return out


def _start_year(t: Trial) -> list[Category]:
    year = t.start_year
    if year is None:
        return []
    return [Category(str(year), None, Evidence(field=P_START, value=t.start_date or ""))]


SPECS: dict[Dimension, DimensionSpec] = {
    Dimension.PHASE: DimensionSpec(Dimension.PHASE, "phase", "Phase", False, PHASE_LABEL_ORDER, None, _phase),
    Dimension.STATUS: DimensionSpec(
        Dimension.STATUS,
        "status",
        "Overall status",
        False,
        [STATUS_LABELS[s.value] for s in OverallStatus] + [MISSING],
        None,
        _status,
    ),
    Dimension.STUDY_TYPE: DimensionSpec(
        Dimension.STUDY_TYPE,
        "study_type",
        "Study type",
        False,
        [STUDY_TYPE_LABELS[s.value] for s in StudyType] + [MISSING],
        None,
        _study_type,
    ),
    Dimension.SPONSOR_CLASS: DimensionSpec(
        Dimension.SPONSOR_CLASS, "sponsor_class", "Lead sponsor class", False, None, None, _sponsor_class
    ),
    Dimension.SPONSOR: DimensionSpec(Dimension.SPONSOR, "sponsor", "Lead sponsor", False, None, 15, _sponsor),
    Dimension.COUNTRY: DimensionSpec(Dimension.COUNTRY, "country", "Country", True, None, 20, _country),
    Dimension.INTERVENTION_TYPE: DimensionSpec(
        Dimension.INTERVENTION_TYPE, "intervention_type", "Intervention type", True, None, None, _intervention_type
    ),
    Dimension.CONDITION: DimensionSpec(Dimension.CONDITION, "condition", "Condition", True, None, 15, _condition),
    Dimension.INTERVENTION: DimensionSpec(Dimension.INTERVENTION, "intervention", "Drug", True, None, 15, _intervention),
    Dimension.START_YEAR: DimensionSpec(Dimension.START_YEAR, "period", "Start year", False, None, None, _start_year),
}

# Code lists used to label exact server counts with the same display strings.
CODE_LABELS: dict[Dimension, dict[str, str]] = {
    Dimension.STATUS: STATUS_LABELS,
    Dimension.STUDY_TYPE: STUDY_TYPE_LABELS,
    Dimension.SPONSOR_CLASS: {s.value: SPONSOR_CLASS_LABELS[s.value] for s in SponsorClass},
    Dimension.INTERVENTION_TYPE: {t.value: INTERVENTION_TYPE_LABELS[t.value] for t in InterventionType},
}


def spec_for(dimension: Dimension) -> DimensionSpec:
    return SPECS[dimension]
