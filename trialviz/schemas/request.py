"""Request schema for POST /query."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trialviz.schemas.enums import OverallStatus, Phase, StudyType
from trialviz.schemas.plan import EntityFilter


class QueryRequest(BaseModel):
    """A natural-language question plus optional structured constraints.

    Structured fields are authoritative: when both the question and a field
    mention a drug, the field wins. They are merged into the planner's base
    filter before retrieval, so they also apply to every series of a comparison
    unless a series overrides that field.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "How has the number of trials for this drug changed per year since 2015?",
                    "drug_name": "Pembrolizumab",
                },
                {
                    "query": "Show a network of sponsors and drugs for melanoma trials",
                    "max_trials": 1500,
                    "citations_per_datum": 3,
                },
            ]
        },
    )

    query: str = Field(..., min_length=3, max_length=1000, description="Natural-language question about clinical trials")

    drug_name: str | None = Field(None, max_length=200, description="Intervention name; searched in the API's intervention area")
    condition: str | None = Field(None, max_length=200, description="Condition or disease; searched in the API's condition area")
    sponsor: str | None = Field(None, max_length=200, description="Sponsor or collaborator organization name")
    country: str | None = Field(
        None, max_length=100, description="Country name as spelled on ClinicalTrials.gov, e.g. 'United States'"
    )
    phase: list[Phase] | None = Field(None, description="Restrict to trials listing any of these phases")
    status: list[OverallStatus] | None = Field(None, description="Restrict to trials with any of these overall statuses")
    study_type: StudyType | None = Field(None, description="INTERVENTIONAL, OBSERVATIONAL, or EXPANDED_ACCESS")
    start_year: int | None = Field(None, ge=1900, le=2100, description="Earliest start year, inclusive")
    end_year: int | None = Field(None, ge=1900, le=2100, description="Latest start year, inclusive")

    max_trials: int = Field(
        2000,
        ge=50,
        le=10000,
        description="Upper bound on trial records fetched per series. Larger result sets are sampled "
        "(most recently started first) and flagged as truncated in meta.retrieval.",
    )
    include_citations: bool = Field(True, description="Attach per-datum citations to source trial records")
    citations_per_datum: int = Field(
        5, ge=0, le=50, description="Maximum citations attached to each bar, bucket, point, node, or edge"
    )
    count_mode: Literal["auto", "sample", "exact"] = Field(
        "auto",
        description="How bar heights are counted when the result set exceeds max_trials: 'exact' asks the API "
        "for one count per category, 'sample' counts the fetched records, 'auto' uses exact counts only "
        "when the sample would be truncated and the dimension supports it.",
    )
    planner: Literal["auto", "llm", "rules"] = Field(
        "auto",
        description="'llm' requires an Anthropic API key, 'rules' is the deterministic fallback, 'auto' tries llm then rules",
    )

    @model_validator(mode="after")
    def _years_in_order(self) -> QueryRequest:
        if self.start_year is not None and self.end_year is not None and self.end_year < self.start_year:
            raise ValueError("end_year must be greater than or equal to start_year")
        return self

    def as_filter(self) -> EntityFilter:
        """The structured fields expressed as an EntityFilter."""
        return EntityFilter(
            drugs=[self.drug_name] if self.drug_name else [],
            conditions=[self.condition] if self.condition else [],
            sponsors=[self.sponsor] if self.sponsor else [],
            countries=[self.country] if self.country else [],
            phases=list(self.phase or []),
            statuses=list(self.status or []),
            study_types=[self.study_type] if self.study_type else [],
            start_year=self.start_year,
            end_year=self.end_year,
        )
