"""Post-build sanity checks on a visualization spec.

These guard against bugs in the aggregation and assembly layers, not against
bad input. A failure here is a server-side error, so it raises rather than
warns: shipping a chart whose citations do not match its bars would be worse
than returning 500.
"""

from __future__ import annotations

from trialviz.schemas.response import NetworkGraphSpec, VisualizationSpec


class SpecInvalid(Exception):
    pass


def check_spec(spec: VisualizationSpec, fetched_ids: set[str], *, expected_sum: int | None = None) -> None:
    if isinstance(spec, NetworkGraphSpec):
        _check_network(spec, fetched_ids)
        return

    x_field, y_field = spec.encoding.x.field, spec.encoding.y.field
    declared = {c.name for c in spec.columns}
    if x_field not in declared or y_field not in declared:
        raise SpecInvalid(f"encoding fields {x_field!r}/{y_field!r} are not documented in columns")
    total = 0
    for i, row in enumerate(spec.data):
        values = row.model_dump()
        if x_field not in values or y_field not in values:
            raise SpecInvalid(f"row {i} lacks the encoded fields {x_field!r}/{y_field!r}")
        y = values[y_field]
        if not isinstance(y, (int, float)) or y < 0:
            raise SpecInvalid(f"row {i} has a non-numeric or negative value for {y_field!r}: {y!r}")
        if spec.encoding.color and spec.encoding.color.field not in values:
            raise SpecInvalid(f"row {i} lacks the color field {spec.encoding.color.field!r}")
        undeclared = set(values) - declared
        if undeclared:
            raise SpecInvalid(f"row {i} has undocumented keys {sorted(undeclared)}")
        _check_citations(row.citations, row.citation_count, fetched_ids, f"row {i}")
        total += int(y)
    if expected_sum is not None and total != expected_sum:
        raise SpecInvalid(f"values sum to {total} but {expected_sum} trials were categorized")


def _check_network(spec: NetworkGraphSpec, fetched_ids: set[str]) -> None:
    ids = {n.id for n in spec.data.nodes}
    if len(ids) != len(spec.data.nodes):
        raise SpecInvalid("duplicate node ids")
    for n in spec.data.nodes:
        if n.kind not in spec.node_kinds:
            raise SpecInvalid(f"node {n.id} has kind {n.kind!r} not listed in node_kinds")
        _check_citations(n.citations, n.citation_count, fetched_ids, f"node {n.id}")
    for e in spec.data.edges:
        if e.source not in ids or e.target not in ids:
            raise SpecInvalid(f"edge {e.source} -> {e.target} references an unknown node")
        if e.source == e.target:
            raise SpecInvalid(f"self-loop on {e.source}")
        _check_citations(e.citations, e.citation_count, fetched_ids, f"edge {e.source}->{e.target}")


def _check_citations(citations, citation_count: int, fetched_ids: set[str], where: str) -> None:
    if citation_count < len(citations):
        raise SpecInvalid(f"{where}: citation_count {citation_count} is smaller than the {len(citations)} citations attached")
    seen: set[str] = set()
    for c in citations:
        if c.nct_id not in fetched_ids:
            raise SpecInvalid(f"{where}: citation {c.nct_id} is not among the fetched trials")
        if c.nct_id in seen:
            raise SpecInvalid(f"{where}: citation {c.nct_id} appears twice")
        seen.add(c.nct_id)
        if not c.evidence:
            raise SpecInvalid(f"{where}: citation {c.nct_id} carries no evidence")
