"""Co-occurrence networks between entities that appear in the same trial.

A network kind names two entity roles (or one role twice). For every trial,
each entity of the first role is linked to each entity of the second role, and
the edge weight is the number of distinct trials in which the pair co-occurs.
Drug names are folded with `DrugCanonicalizer`; conditions prefer the MeSH
term from `conditionBrowseModule` over free text when one is available.

Graphs are pruned to the heaviest edges so they stay readable: the goal is a
picture of the main relationships, with citations to the trials behind each
edge, not an exhaustive dump.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations, product

from trialviz.analysis.drugs import DrugCanonicalizer
from trialviz.ctgov.records import (
    P_COLLABORATORS,
    P_CONDITION_MESH,
    P_CONDITIONS,
    P_INTERVENTIONS,
    P_LEAD_SPONSOR,
    Trial,
)
from trialviz.schemas.enums import NetworkKind
from trialviz.schemas.response import Citation, Edge, Evidence, Node

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
    trials_with_pairs: int  # trials that contributed at least one edge
    edges_before_trim: int


ROLES: dict[NetworkKind, tuple[str, str]] = {
    NetworkKind.SPONSOR_DRUG: ("sponsor", "drug"),
    NetworkKind.DRUG_DRUG: ("drug", "drug"),
    NetworkKind.CONDITION_DRUG: ("condition", "drug"),
    NetworkKind.SPONSOR_CONDITION: ("sponsor", "condition"),
    NetworkKind.SPONSOR_COLLABORATOR: ("sponsor", "collaborator"),
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


def build_network(
    trials: Sequence[Trial],
    kind: NetworkKind,
    *,
    max_edges: int | None = None,
    citations_per_datum: int = 5,
) -> NetworkResult:
    role_a, role_b = ROLES[kind]
    canon = DrugCanonicalizer(trials)
    nodes: dict[str, _NodeAcc] = {}
    edges: dict[tuple[str, str], _EdgeAcc] = {}
    trials_with_pairs = 0

    for trial in trials:
        ents_a = entities_for(trial, role_a, canon)
        pairs: list[tuple[Entity, Entity]]
        if role_a == role_b:
            pairs = list(combinations(sorted(ents_a, key=lambda e: e.key), 2))
        else:
            pairs = list(product(ents_a, entities_for(trial, role_b, canon)))
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
            citations=_citations_edges(e, citations_per_datum),
            citation_count=len(e.trials),
        )
        for e in ranked
    ]
    kinds = [role_a] if role_a == role_b else [role_a, role_b]
    return NetworkResult(
        nodes=out_nodes, edges=out_edges, node_kinds=kinds, trials_with_pairs=trials_with_pairs, edges_before_trim=edges_before
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
