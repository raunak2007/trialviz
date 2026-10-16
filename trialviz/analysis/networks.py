"""Co-occurrence networks between entities that appear in the same trial.

A network kind names two entity roles (or one role twice). For every trial,
each entity of the first role is linked to each entity of the second role, and
the edge weight is the number of distinct trials in which the pair co-occurs.
Drug names are folded with `DrugCanonicalizer`; conditions prefer the MeSH
term from `conditionBrowseModule` over free text when one is available.

Two drug-to-drug kinds exist because "co-occur in a trial" and "given in
combination" are different claims. A trial comparing pembrolizumab against
docetaxel lists both drugs, but in different arms. `drug_drug` links drugs
only when they are listed in the same arm group (a combination); trials
without arm data cannot contribute and are counted. `drug_drug_same_trial`
links any two drugs listed anywhere in the same trial.

Graphs are pruned to the heaviest edges so they stay readable: the goal is a
picture of the main relationships, with citations to the trials behind each
edge, not an exhaustive dump.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations, product

from trialviz.analysis.drugs import DrugCanonicalizer, normalize_name
from trialviz.ctgov.records import (
    P_ARM_GROUPS,
    P_COLLABORATORS,
    P_CONDITION_MESH,
    P_CONDITIONS,
    P_INTERVENTIONS,
    P_LEAD_SPONSOR,
    Trial,
)
from trialviz.schemas.enums import NetworkKind
from trialviz.schemas.response import Citation, Edge, Evidence, Exactness, Node

DEFAULT_MAX_EDGES = 50


@dataclass(frozen=True)
class Entity:
    kind: str  # "drug", "sponsor", "condition", "collaborator"
    key: str  # identity (case-folded canonical name)
    label: str
    evidence: Evidence

    @property
    def node_id(self) -> str:
        return f"{self.kind}:{self.key}"


@dataclass
class _NodeAcc:
    entity: Entity
    trials: dict[str, tuple[Trial, Evidence]] = field(default_factory=dict)


@dataclass
class _EdgeAcc:
    a: Entity
    b: Entity
    trials: dict[str, tuple[Trial, list[Evidence]]] = field(default_factory=dict)


@dataclass
class NetworkResult:
    nodes: list[Node]
    edges: list[Edge]
    node_kinds: list[str]
    edge_semantics: str
    trials_considered: int  # trials that had the data needed to contribute
    trials_with_pairs: int  # trials that contributed at least one edge
    trials_without_arm_data: int  # drug_drug only: trials skipped because they list no arm groups
    edges_before_trim: int


ROLES: dict[NetworkKind, tuple[str, str]] = {
    NetworkKind.SPONSOR_DRUG: ("sponsor", "drug"),
    NetworkKind.DRUG_DRUG: ("drug", "drug"),
    NetworkKind.DRUG_DRUG_SAME_TRIAL: ("drug", "drug"),
    NetworkKind.CONDITION_DRUG: ("condition", "drug"),
    NetworkKind.SPONSOR_CONDITION: ("sponsor", "condition"),
    NetworkKind.SPONSOR_COLLABORATOR: ("sponsor", "collaborator"),
}

EDGE_SEMANTICS: dict[NetworkKind, str] = {
    NetworkKind.SPONSOR_DRUG: "the lead sponsor ran a trial that lists the drug as an intervention",
    NetworkKind.DRUG_DRUG: "both drugs are listed in the same arm group of a trial (given in combination)",
    NetworkKind.DRUG_DRUG_SAME_TRIAL: "both drugs are listed anywhere in the same trial, including as comparators",
    NetworkKind.CONDITION_DRUG: "a trial for the condition lists the drug as an intervention",
    NetworkKind.SPONSOR_CONDITION: "the lead sponsor ran a trial for the condition",
    NetworkKind.SPONSOR_COLLABORATOR: "the organization is listed as a collaborator on the lead sponsor's trial",
}


def _drugs(trial: Trial, canon: DrugCanonicalizer) -> list[Entity]:
    seen: dict[str, Entity] = {}
    for i in trial.drug_interventions():
        label = canon.canonical(i.name)
        key = label.casefold()
        if key not in seen:
            seen[key] = Entity("drug", key, label, Evidence(field=f"{P_INTERVENTIONS}[{i.index}].name", value=i.name))
    return list(seen.values())


def _sponsor(trial: Trial) -> list[Entity]:
    if not trial.lead_sponsor:
        return []
    return [
        Entity(
            "sponsor", trial.lead_sponsor.casefold(), trial.lead_sponsor, Evidence(field=P_LEAD_SPONSOR, value=trial.lead_sponsor)
        )
    ]


def _collaborators(trial: Trial) -> list[Entity]:
    out: dict[str, Entity] = {}
    for idx, name in enumerate(trial.collaborators):
        key = name.casefold()
        if key not in out:
            out[key] = Entity("collaborator", key, name, Evidence(field=f"{P_COLLABORATORS}[{idx}].name", value=name))
    return list(out.values())


def _conditions(trial: Trial) -> list[Entity]:
    out: dict[str, Entity] = {}
    if trial.condition_mesh_terms:
        for idx, term in enumerate(trial.condition_mesh_terms):
            key = term.casefold()
            if key not in out:
                out[key] = Entity("condition", key, term, Evidence(field=f"{P_CONDITION_MESH}[{idx}].term", value=term))
    else:
        for idx, name in enumerate(trial.conditions):
            key = name.casefold()
            if key not in out:
                out[key] = Entity("condition", key, name, Evidence(field=f"{P_CONDITIONS}[{idx}]", value=name))
    return list(out.values())


def entities_for(trial: Trial, role: str, canon: DrugCanonicalizer) -> list[Entity]:
    if role == "drug":
        return _drugs(trial, canon)
    if role == "sponsor":
        return _sponsor(trial)
    if role == "collaborator":
        return _collaborators(trial)
    if role == "condition":
        return _conditions(trial)
    raise ValueError(f"unknown role {role}")


def same_arm_pairs(trial: Trial, canon: DrugCanonicalizer) -> list[tuple[Entity, Entity]]:
    """Drug pairs listed together in at least one arm group of the trial.

    Arm entries name interventions by their listed name ("Drug: Pembrolizumab"); they are matched
    to the trial's drug interventions by normalized name, then folded to canonical entities.
    """
    drugs = {normalize_name(i.name): i for i in trial.drug_interventions()}
    entities = {e.key: e for e in _drugs(trial, canon)}
    pairs: dict[tuple[str, str], tuple[Entity, Entity]] = {}
    for arm_index, arm in enumerate(trial.arm_interventions):
        in_arm: dict[str, Entity] = {}
        for name in arm:
            intervention = drugs.get(normalize_name(name))
            if intervention is None:
                continue
            key = canon.canonical(intervention.name).casefold()
            entity = entities.get(key)
            if entity is not None and key not in in_arm:
                in_arm[key] = Entity(
                    entity.kind,
                    entity.key,
                    entity.label,
                    Evidence(field=f"{P_ARM_GROUPS}[{arm_index}].interventionNames", value=name),
                )
        for a, b in combinations(sorted(in_arm.values(), key=lambda e: e.key), 2):
            pairs.setdefault((a.key, b.key), (a, b))
    return list(pairs.values())


def build_network(
    trials: Sequence[Trial],
    kind: NetworkKind,
    *,
    max_edges: int | None = None,
    citations_per_datum: int = 5,
    exactness: Exactness = "exact",
) -> NetworkResult:
    role_a, role_b = ROLES[kind]
    canon = DrugCanonicalizer(trials)
    nodes: dict[str, _NodeAcc] = {}
    edges: dict[tuple[str, str], _EdgeAcc] = {}
    trials_with_pairs = 0
    trials_considered = 0
    without_arm_data = 0

    for trial in trials:
        pairs: list[tuple[Entity, Entity]]
        if kind == NetworkKind.DRUG_DRUG:
            if not trial.has_arm_data:
                without_arm_data += 1
                continue
            pairs = same_arm_pairs(trial, canon)
        elif role_a == role_b:
            pairs = list(combinations(sorted(entities_for(trial, role_a, canon), key=lambda e: e.key), 2))
        else:
            pairs = list(product(entities_for(trial, role_a, canon), entities_for(trial, role_b, canon)))
        trials_considered += 1
        if not pairs:
            continue
        trials_with_pairs += 1
        for a, b in pairs:
            key = (a.node_id, b.node_id)
            acc = edges.get(key)
            if acc is None:
                acc = edges[key] = _EdgeAcc(a=a, b=b)
            acc.trials.setdefault(trial.nct_id, (trial, [a.evidence, b.evidence]))
            for ent in (a, b):
                node = nodes.get(ent.node_id)
                if node is None:
                    node = nodes[ent.node_id] = _NodeAcc(entity=ent)
                node.trials.setdefault(trial.nct_id, (trial, ent.evidence))

    edges_before = len(edges)
    limit = max_edges if max_edges is not None else DEFAULT_MAX_EDGES
    ranked = sorted(edges.values(), key=lambda e: (-len(e.trials), e.a.label, e.b.label))[:limit]
    kept_ids = {e.a.node_id for e in ranked} | {e.b.node_id for e in ranked}

    out_nodes = [
        Node(
            id=nid,
            label=acc.entity.label,
            kind=acc.entity.kind,
            trial_count=len(acc.trials),
            exactness=exactness,
            citations=_citations_nodes(acc, citations_per_datum),
            citation_count=len(acc.trials),
        )
        for nid, acc in nodes.items()
        if nid in kept_ids
    ]
    out_nodes.sort(key=lambda n: (n.kind, -n.trial_count, n.label))
    out_edges = [
        Edge(
            source=e.a.node_id,
            target=e.b.node_id,
            trial_count=len(e.trials),
            exactness=exactness,
            citations=_citations_edges(e, citations_per_datum),
            citation_count=len(e.trials),
        )
        for e in ranked
    ]
    kinds = [role_a] if role_a == role_b else [role_a, role_b]
    return NetworkResult(
        nodes=out_nodes,
        edges=out_edges,
        node_kinds=kinds,
        edge_semantics=EDGE_SEMANTICS[kind],
        trials_considered=trials_considered,
        trials_with_pairs=trials_with_pairs,
        trials_without_arm_data=without_arm_data,
        edges_before_trim=edges_before,
    )


def _citations_nodes(acc: _NodeAcc, limit: int) -> list[Citation]:
    return [
        Citation(nct_id=t.nct_id, title=t.brief_title, url=t.url, evidence=[t.evidence_title(), ev])
        for t, ev in list(acc.trials.values())[:limit]
    ]


def _citations_edges(acc: _EdgeAcc, limit: int) -> list[Citation]:
    return [
        Citation(nct_id=t.nct_id, title=t.brief_title, url=t.url, evidence=[t.evidence_title(), *ev])
        for t, ev in list(acc.trials.values())[:limit]
    ]
