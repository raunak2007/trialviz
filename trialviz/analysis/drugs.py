"""Folding of intervention names into canonical drug names.

Registry intervention names are free text: "Pembrolizumab", "pembrolizumab
200 mg", "MK-3475", and "Keytruda" are the same drug. Two deterministic steps
fold them together:

1. Surface normalization: case, whitespace, dose/route suffixes, trailing
   parentheticals, and any `otherNames` listed for the intervention.
2. MeSH anchoring: the API's `interventionBrowseModule.meshes` gives the
   controlled-vocabulary term(s) for a trial's interventions. When exactly one
   MeSH term is a substring of an intervention's name or other names, that
   name is mapped to the MeSH term. The mapping is learned from the result set
   itself, so brand names seen alongside a MeSH term fold correctly without a
   hand-maintained synonym table.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from trialviz.ctgov.records import Intervention, Trial

_DOSE_RE = re.compile(
    r"[\s,]*\b\d+(?:[.,]\d+)?\s*(?:mg|mcg|µg|ug|g|ml|mL|iu|units?|%|mg/kg|mg/m2|mg/m²|mcg/kg)\b.*$",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")
_ROUTE_RE = re.compile(r"\b(oral|iv|intravenous|subcutaneous|sc|tablets?|capsules?|injection|infusion)\b", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def clean_name(name: str) -> str:
    """Display form with dose, route, and trailing parenthetical noise removed; case preserved."""
    s = name.strip()
    s = _DOSE_RE.sub("", s)
    s = _PAREN_RE.sub("", s)
    s = _ROUTE_RE.sub(" ", s)
    s = s.strip(" -,:;/")
    s = _SPACE_RE.sub(" ", s)
    return s or name.strip()


def normalize_name(name: str) -> str:
    """Case-folded identity key for an intervention name."""
    return clean_name(name).casefold()


class DrugCanonicalizer:
    """Maps intervention names to a canonical display name for one result set."""

    def __init__(self, trials: Iterable[Trial]):
        self._alias: dict[str, str] = {}  # normalized name -> canonical display name
        self._display: dict[str, Counter[str]] = defaultdict(Counter)  # normalized -> seen spellings
        self._learn(trials)

    def _learn(self, trials: Iterable[Trial]) -> None:
        for trial in trials:
            mesh_terms = list(dict.fromkeys(trial.intervention_mesh_terms))
            for intervention in trial.drug_interventions():
                key = normalize_name(intervention.name)
                self._display[key][clean_name(intervention.name)] += 1
                matched = self._matching_mesh(intervention, mesh_terms)
                if matched is not None:
                    self._alias[key] = matched
                    for other in intervention.other_names:
                        self._alias.setdefault(normalize_name(other), matched)

    @staticmethod
    def _matching_mesh(intervention: Intervention, mesh_terms: list[str]) -> str | None:
        haystacks = [intervention.name.casefold(), *(o.casefold() for o in intervention.other_names)]
        matches = [term for term in mesh_terms if any(term.casefold() in h for h in haystacks)]
        return matches[0] if len(matches) == 1 else None

    def canonical(self, name: str) -> str:
        key = normalize_name(name)
        if key in self._alias:
            return self._alias[key]
        spellings = self._display.get(key)
        if spellings:
            return spellings.most_common(1)[0][0]
        return clean_name(name)

    def canonical_key(self, name: str) -> str:
        return self.canonical(name).casefold()
