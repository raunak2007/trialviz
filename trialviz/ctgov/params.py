"""Translate an EntityFilter into ClinicalTrials.gov v2 query parameters.

The API has two kinds of constraints: `query.*` search areas (free text with
synonym expansion, e.g. `query.intr`, `query.cond`) and Essie expressions in
`filter.advanced` (`AREA[Field]value`, with AND / OR / NOT and
`RANGE[min,max]` for dates). Everything deterministic lives here so the
request a plan produces can be unit-tested without the network.
"""

from __future__ import annotations

from collections.abc import Iterable

from trialviz.schemas.plan import EntityFilter

# Piece names accepted by the `fields` parameter. Keeping the payload to these
# fields cuts the per-study size by roughly an order of magnitude.
STUDY_FIELDS = (
    "NCTId",
    "BriefTitle",
    "OverallStatus",
    "StartDate",
    "PrimaryCompletionDate",
    "CompletionDate",
    "Phase",
    "StudyType",
    "EnrollmentCount",
    "LeadSponsorName",
    "LeadSponsorClass",
    "CollaboratorName",
    "Condition",
    "InterventionType",
    "InterventionName",
    "InterventionOtherName",
    "LocationCountry",
    "InterventionMeshTerm",
    "ConditionMeshTerm",
)

DEFAULT_SORT = "StartDate:desc"


def essie_term(value: str) -> str:
    """Quote a value for use inside an Essie expression when it contains spaces or punctuation."""
    cleaned = value.replace('"', "").strip()
    if any(ch in cleaned for ch in " ,()[]:"):
        return f'"{cleaned}"'
    return cleaned


def essie_area(field: str, value: str) -> str:
    return f"AREA[{field}]{essie_term(value)}"


def essie_or(clauses: Iterable[str]) -> str:
    items = [c for c in clauses if c]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return "(" + " OR ".join(items) + ")"


def essie_and(clauses: Iterable[str]) -> str:
    items = [c for c in clauses if c]
    return " AND ".join(items)


def essie_date_range(start_year: int | None, end_year: int | None) -> str:
    """Essie RANGE over StartDate; either bound may be open."""
    if start_year is None and end_year is None:
        return ""
    lo = f"{start_year:04d}-01-01" if start_year is not None else "MIN"
    hi = f"{end_year:04d}-12-31" if end_year is not None else "MAX"
    return f"AREA[StartDate]RANGE[{lo},{hi}]"


def search_text(terms: list[str]) -> str:
    """Join free-text terms for a `query.*` area: a single term is passed through, several are OR-ed."""
    cleaned = [t.strip() for t in terms if t and t.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return " OR ".join(f"({t})" if " " in t else t for t in cleaned)


def advanced_filter(f: EntityFilter, extra: str = "") -> str:
    """The `filter.advanced` expression for a filter, optionally AND-ed with an extra clause."""
    clauses = [
        essie_or(essie_area("LocationCountry", c) for c in f.countries),
        essie_or(essie_area("Phase", p.value) for p in f.phases),
        essie_or(essie_area("StudyType", s.value) for s in f.study_types),
        essie_date_range(f.start_year, f.end_year),
        extra,
    ]
    return essie_and(clauses)


def build_search_params(
    f: EntityFilter,
    *,
    page_size: int,
    fields: Iterable[str] = STUDY_FIELDS,
    sort: str | None = DEFAULT_SORT,
    count_total: bool = True,
    extra_advanced: str = "",
) -> dict[str, str]:
    """Parameters for GET /studies (without pageToken, which the client adds per page)."""
    params: dict[str, str] = {"format": "json", "pageSize": str(page_size)}
    if drugs := search_text(f.drugs):
        params["query.intr"] = drugs
    if conditions := search_text(f.conditions):
        params["query.cond"] = conditions
    if sponsors := search_text(f.sponsors):
        params["query.spons"] = sponsors
    if terms := search_text(f.terms):
        params["query.term"] = terms
    if f.statuses:
        params["filter.overallStatus"] = ",".join(s.value for s in f.statuses)
    if advanced := advanced_filter(f, extra_advanced):
        params["filter.advanced"] = advanced
    field_list = ",".join(fields)
    if field_list:
        params["fields"] = field_list
    if sort:
        params["sort"] = sort
    if count_total:
        params["countTotal"] = "true"
    return params


def build_count_params(f: EntityFilter, extra_advanced: str = "") -> dict[str, str]:
    """Parameters for a count-only request: one record, one field, with totalCount."""
    return build_search_params(
        f,
        page_size=1,
        fields=("NCTId",),
        sort=None,
        count_total=True,
        extra_advanced=extra_advanced,
    )
