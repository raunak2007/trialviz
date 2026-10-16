"""Normalization of raw ClinicalTrials.gov v2 study records.

A `Trial` holds the handful of fields the analyses need, plus enough positional
information to point back at the exact JSON path a value came from. Everything
that is not needed for grouping, measuring, or citing is dropped on parse so
memory stays flat when thousands of records are in flight.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from trialviz.schemas.response import Evidence

STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"

# JSON paths inside a study record, used both for parsing and for citations.
P_NCT = "protocolSection.identificationModule.nctId"
P_TITLE = "protocolSection.identificationModule.briefTitle"
P_STATUS = "protocolSection.statusModule.overallStatus"
P_START = "protocolSection.statusModule.startDateStruct.date"
P_COMPLETION = "protocolSection.statusModule.completionDateStruct.date"
P_PRIMARY_COMPLETION = "protocolSection.statusModule.primaryCompletionDateStruct.date"
P_PHASES = "protocolSection.designModule.phases"
P_STUDY_TYPE = "protocolSection.designModule.studyType"
P_ENROLLMENT = "protocolSection.designModule.enrollmentInfo.count"
P_LEAD_SPONSOR = "protocolSection.sponsorCollaboratorsModule.leadSponsor.name"
P_LEAD_SPONSOR_CLASS = "protocolSection.sponsorCollaboratorsModule.leadSponsor.class"
P_COLLABORATORS = "protocolSection.sponsorCollaboratorsModule.collaborators"
P_CONDITIONS = "protocolSection.conditionsModule.conditions"
P_INTERVENTIONS = "protocolSection.armsInterventionsModule.interventions"
P_LOCATIONS = "protocolSection.contactsLocationsModule.locations"
P_ARM_GROUPS = "protocolSection.armsInterventionsModule.armGroups"
P_INTERVENTION_MESH = "derivedSection.interventionBrowseModule.meshes"
P_CONDITION_MESH = "derivedSection.conditionBrowseModule.meshes"

# Display labels for the phase enumeration, in the order the registry uses.
PHASE_LABELS = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    "NA": "Not Applicable",
}
PHASE_NOT_SPECIFIED = "Not specified"
PHASE_LABEL_ORDER = [
    "Early Phase 1",
    "Phase 1",
    "Phase 1/Phase 2",
    "Phase 2",
    "Phase 2/Phase 3",
    "Phase 3",
    "Phase 4",
    "Not Applicable",
    PHASE_NOT_SPECIFIED,
]

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")


@dataclass(frozen=True)
class Intervention:
    index: int  # position in the interventions array, for citation paths
    name: str
    type: str | None
    other_names: tuple[str, ...] = ()


@dataclass
class Trial:
    nct_id: str
    brief_title: str | None = None
    overall_status: str | None = None
    start_date: str | None = None
    completion_date: str | None = None
    phases: tuple[str, ...] = ()
    study_type: str | None = None
    enrollment: int | None = None
    lead_sponsor: str | None = None
    lead_sponsor_class: str | None = None
    collaborators: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    interventions: tuple[Intervention, ...] = ()
    countries: tuple[str, ...] = ()
    country_location_index: dict[str, int] = field(default_factory=dict)
    site_count: int = 0
    # One entry per arm group: the intervention names listed for that arm, prefix ("Drug: ") removed.
    arm_interventions: tuple[tuple[str, ...], ...] = ()
    intervention_mesh_terms: tuple[str, ...] = ()
    condition_mesh_terms: tuple[str, ...] = ()

    # ---- derived values -------------------------------------------------

    @property
    def url(self) -> str:
        return STUDY_URL.format(nct_id=self.nct_id)

    @property
    def start_year(self) -> int | None:
        parsed = parse_partial_date(self.start_date)
        return parsed.year if parsed else None

    @property
    def start_month(self) -> int | None:
        parsed = parse_partial_date(self.start_date)
        return parsed.month if parsed else None

    @property
    def phase_label(self) -> str:
        """Single display bucket per trial, following the registry's own convention.

        Combined designations such as ["PHASE1", "PHASE2"] become "Phase 1/Phase 2"
        so every trial lands in exactly one bucket and bucket counts add up.
        """
        if not self.phases:
            return PHASE_NOT_SPECIFIED
        ordered = [p for p in PHASE_LABELS if p in self.phases]
        if not ordered:
            return PHASE_NOT_SPECIFIED
        return "/".join(PHASE_LABELS[p] for p in ordered)

    @property
    def duration_months(self) -> float | None:
        start = parse_partial_date(self.start_date)
        end = parse_partial_date(self.completion_date)
        if start is None or end is None or end < start:
            return None
        return round((end - start).days / 30.4375, 1)

    @property
    def has_arm_data(self) -> bool:
        return any(self.arm_interventions)

    def mentions_drug(self, term: str) -> bool:
        """True when `term` appears in an intervention name, an other name, or an intervention MeSH term."""
        needle = term.casefold().strip()
        if not needle:
            return False
        haystacks = [i.name for i in self.interventions]
        haystacks += [o for i in self.interventions for o in i.other_names]
        haystacks += list(self.intervention_mesh_terms)
        return any(needle in h.casefold() for h in haystacks)

    def drug_interventions(self) -> list[Intervention]:
        """Interventions that are drugs or biologicals, excluding placebo-like controls."""
        return [
            i
            for i in self.interventions
            if i.type in ("DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", None) and not is_control_intervention(i.name)
        ]

    # ---- citation helpers -------------------------------------------------

    def evidence_title(self) -> Evidence:
        return Evidence(field=P_TITLE, value=self.brief_title or "")


_CONTROL_RE = re.compile(
    r"^(placebo|placebos|sham|standard of care|standard care|best supportive care|no intervention|"
    r"control|vehicle|saline|normal saline|usual care|observation)\b",
    re.IGNORECASE,
)


def is_control_intervention(name: str) -> bool:
    """True for placebo, sham, and standard-of-care arms that would pollute drug networks."""
    stripped = name.strip()
    if not stripped:
        return True
    return bool(_CONTROL_RE.match(stripped)) or "placebo" in stripped.lower()


_PREFIX_RE = re.compile(r"^\s*[A-Za-z][A-Za-z _/-]{1,30}:\s+")


def strip_intervention_prefix(name: str) -> str:
    """Arm group entries read 'Drug: Pembrolizumab'; return the name without the type prefix."""
    return _PREFIX_RE.sub("", name, count=1).strip()


def parse_partial_date(value: str | None) -> date | None:
    """Parse 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD'. Missing parts default to the first day."""
    if not value:
        return None
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def encode_value(value: Any) -> str:
    """Render an API value exactly: strings as they are, anything else as compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _get(obj: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_study(raw: dict[str, Any]) -> Trial:
    """Build a Trial from one element of the API's `studies` array.

    Tolerant by design: the API omits modules that have no data, and `fields`
    pruning removes everything we did not ask for.
    """
    nct_id = _get(raw, P_NCT)
    if not nct_id:
        raise ValueError("study record has no nctId")

    interventions: list[Intervention] = []
    for idx, item in enumerate(_get(raw, P_INTERVENTIONS, []) or []):
        name = (item.get("name") or "").strip()
        if not name:
            continue
        other = tuple(str(o).strip() for o in item.get("otherNames", []) or [] if str(o).strip())
        interventions.append(Intervention(index=idx, name=name, type=item.get("type"), other_names=other))

    countries: list[str] = []
    first_index: dict[str, int] = {}
    locations = _get(raw, P_LOCATIONS, []) or []
    for idx, loc in enumerate(locations):
        country = (loc.get("country") or "").strip()
        if country and country not in first_index:
            first_index[country] = idx
            countries.append(country)

    arms: list[tuple[str, ...]] = []
    for arm in _get(raw, P_ARM_GROUPS, []) or []:
        names = tuple(strip_intervention_prefix(n) for n in (arm.get("interventionNames") or []) if isinstance(n, str))
        arms.append(tuple(n for n in names if n))

    phases = tuple(p for p in (_get(raw, P_PHASES, []) or []) if isinstance(p, str))
    collaborators = tuple(
        (c.get("name") or "").strip() for c in (_get(raw, P_COLLABORATORS, []) or []) if (c.get("name") or "").strip()
    )
    conditions = tuple(str(c).strip() for c in (_get(raw, P_CONDITIONS, []) or []) if str(c).strip())
    intervention_mesh = tuple(
        (m.get("term") or "").strip() for m in (_get(raw, P_INTERVENTION_MESH, []) or []) if (m.get("term") or "").strip()
    )
    condition_mesh = tuple(
        (m.get("term") or "").strip() for m in (_get(raw, P_CONDITION_MESH, []) or []) if (m.get("term") or "").strip()
    )

    return Trial(
        nct_id=str(nct_id),
        brief_title=_get(raw, P_TITLE),
        overall_status=_get(raw, P_STATUS),
        start_date=_get(raw, P_START),
        completion_date=_get(raw, P_COMPLETION) or _get(raw, P_PRIMARY_COMPLETION),
        phases=phases,
        study_type=_get(raw, P_STUDY_TYPE),
        enrollment=_as_int(_get(raw, P_ENROLLMENT)),
        lead_sponsor=_get(raw, P_LEAD_SPONSOR),
        lead_sponsor_class=_get(raw, P_LEAD_SPONSOR_CLASS),
        collaborators=collaborators,
        conditions=conditions,
        interventions=tuple(interventions),
        countries=tuple(countries),
        country_location_index=first_index,
        site_count=len(locations),
        arm_interventions=tuple(arms),
        intervention_mesh_terms=intervention_mesh,
        condition_mesh_terms=condition_mesh,
    )


def parse_studies(studies: list[dict[str, Any]]) -> list[Trial]:
    """Parse a page of studies, skipping malformed entries and de-duplicating by NCT id."""
    seen: set[str] = set()
    out: list[Trial] = []
    for raw in studies:
        try:
            trial = parse_study(raw)
        except ValueError:
            continue
        if trial.nct_id in seen:
            continue
        seen.add(trial.nct_id)
        out.append(trial)
    return out
