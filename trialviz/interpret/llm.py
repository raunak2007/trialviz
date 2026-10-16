"""Language-model planner: the question goes in, a PlanDraft comes out.

The model is used for exactly one thing, reading the question. The plan is
submitted through a single tool, `submit_query_plan`, whose input schema is
the PlanDraft JSON schema; the tool input is validated by Pydantic before
anything else sees it. The worst a bad generation can do is pick the wrong
menu item, which the normalizer and the user-visible `meta.plan` make easy to
spot and correct.

Two earlier forms were tried against the live API and dropped:

* grammar-constrained structured output (`output_config.format`) rejected this
  schema as too complex, after 20 to 45 seconds per call;
* forced tool choice (`tool_choice: {"type": "tool"}`) is not supported on the
  current model generation (Sonnet 5.5, Opus 5.5, Fable 5.1 return 400).

So the request uses `tool_choice: auto` with a prompt that allows only the tool
call. If the model answers in prose instead, that is a PlannerBadOutput and
the planner asks once more with the reason; a second failure hands over to the
rule-based planner.
"""

from __future__ import annotations

import json
import re
from datetime import date
from enum import StrEnum

import anthropic
from pydantic import ValidationError

from trialviz.schemas.enums import (
    AnalysisType,
    Dimension,
    Measure,
    NetworkKind,
    NumericField,
    OverallStatus,
    Phase,
    StudyType,
    TimeGranularity,
)
from trialviz.schemas.plan import EntityFilter, PlanDraft

DEFAULT_MODEL = "claude-sonnet-5-5"


class PlannerUnavailable(Exception):
    """The model could not be used at all (no key, network, API rejection, refusal)."""


class PlannerBadOutput(Exception):
    """The model answered but its plan did not validate. The message is fed back for one retry."""


TOOL_NAME = "submit_query_plan"

# Enum fields the model occasionally writes in the wrong case or with spaces; values are
# matched to the enum by their alphanumeric characters only ("Phase 3" -> PHASE3,
# "Active, not recruiting" -> ACTIVE_NOT_RECRUITING) before validation.
_ENUM_FIELDS: dict[str, type[StrEnum]] = {
    "analysis": AnalysisType,
    "dimension": Dimension,
    "split_by": Dimension,
    "measure": Measure,
    "network": NetworkKind,
    "numeric_x": NumericField,
    "numeric_y": NumericField,
    "time_granularity": TimeGranularity,
}
_FILTER_LIST_FIELDS: dict[str, type[StrEnum]] = {"phases": Phase, "statuses": OverallStatus, "study_types": StudyType}


def _canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _match_enum(value: object, enum: type[StrEnum]) -> object:
    if not isinstance(value, str):
        return value
    wanted = _canonical(value)
    for member in enum:
        if _canonical(member.value) == wanted:
            return member.value
    return value


def _coerce_case(data: dict) -> dict:
    """Snap enum-valued strings in a raw tool input onto the enum spelling; leave everything else alone."""
    out = dict(data)
    for key, enum in _ENUM_FIELDS.items():
        if key in out:
            out[key] = _match_enum(out[key], enum)
    filters = [out.get("filter"), *[s.get("filter") for s in out.get("series") or [] if isinstance(s, dict)]]
    for container in filters:
        if not isinstance(container, dict):
            continue
        for key, enum in _FILTER_LIST_FIELDS.items():
            if isinstance(container.get(key), list):
                container[key] = [_match_enum(v, enum) for v in container[key]]
    return out


SYSTEM_PROMPT = """You turn questions about clinical trials into a structured query plan for a backend that reads ClinicalTrials.gov.

You never see trial data and you never answer the question yourself. You only decide WHAT to compute, by filling in the plan schema. All numbers come from the registry later.

Respond by calling the submit_query_plan tool exactly once. Do not write prose, do not ask questions, do not explain. If a question cannot be planned, still call the tool with analysis "unsupported" and say why in unsupported_reason.

Today's date is {today}.

How to fill the plan:
- analysis:
  * time_trend: counts per start year (or quarter/month) for one entity. Optional split_by for several lines.
  * distribution: counts across one categorical dimension for one entity (phase, intervention_type, sponsor_class, status, study_type, condition, sponsor, intervention, country).
  * comparison: the same dimension for 2 to 4 entities side by side. Put each entity in `series` with only the fields that differ in its filter; shared constraints go in the base `filter`.
  * geographic: counts per country.
  * network: co-occurrence graph. sponsor_drug (which sponsors test which drugs), drug_drug (drugs given in combination: listed in the same arm of a trial; use for "combination", "combined with", "together"), drug_drug_same_trial (drugs listed anywhere in the same trial, including as comparators; use for "co-occur in the same trial", "appear together in trials"), condition_drug, sponsor_condition, sponsor_collaborator.
  * scatter: one point per trial, numeric_x vs numeric_y (enrollment, start_year, duration_months, site_count). split_by colors the points.
  * histogram: binned distribution of one numeric field in numeric_x.
  * unsupported: the question needs data this backend does not have (outcomes, results, eligibility text, patient-level data, adverse events, averages of fields outside the menu, questions that are not about clinical trials). Explain in unsupported_reason.
- filter: copy entity names verbatim from the question; do not invent, expand, or translate them. Medications, biologics, vaccines, and brand names go in `drugs`; diseases and conditions go in `conditions`; organizations in `sponsors`; countries in `countries`. Use `terms` only when you cannot tell what kind of thing a name is.
- Structured fields supplied with the request are authoritative. Copy them into the filter and do not contradict them. "this drug" or "this condition" refers to them.
- Years: "since 2015" means start_year 2015 and no end_year. "last 5 years" means start_year = {last5} (computed from today). "between 2018 and 2022" sets both. Do not set years the question does not mention.
- "recruiting trials" means statuses [RECRUITING]. "completed trials" means [COMPLETED]. Phase words like "phase 3 trials" are a `phases` filter; "across phases" or "by phase" is dimension phase.
- measure is trial_count unless the question asks about enrollment totals or participants.
- top_n only when the question says "top 10", "most common 5", etc.
- interpretation: one plain sentence saying what will be computed. assumptions: short notes on any choice the question left open.

Examples (question -> plan, abbreviated):
1. "How has the number of trials for pembrolizumab changed per year since 2015?" -> analysis time_trend, filter.drugs ["pembrolizumab"], filter.start_year 2015.
2. "How are melanoma trials distributed across phases?" -> analysis distribution, dimension phase, filter.conditions ["melanoma"].
3. "Compare phases for trials involving nivolumab vs ipilimumab" -> analysis comparison, dimension phase, series [{{label "Nivolumab", filter.drugs ["nivolumab"]}}, {{label "Ipilimumab", filter.drugs ["ipilimumab"]}}].
4. "Compare sponsor categories across asthma and COPD" -> analysis comparison, dimension sponsor_class, series for asthma and COPD using filter.conditions.
5. "Which countries have the most recruiting trials for type 2 diabetes?" -> analysis geographic, filter.conditions ["type 2 diabetes"], filter.statuses ["RECRUITING"].
6. "Show a network of sponsors and drugs for melanoma trials" -> analysis network, network sponsor_drug, filter.conditions ["melanoma"].
7. "Which drugs frequently co-occur in combination studies for lung cancer?" -> analysis network, network drug_drug, filter.conditions ["lung cancer"].
7b. "Which drugs appear in the same trials as pembrolizumab?" -> analysis network, network drug_drug_same_trial, filter.drugs ["pembrolizumab"].
8. "Plot enrollment against start year for breast cancer trials, colored by phase" -> analysis scatter, numeric_x start_year, numeric_y enrollment, split_by phase, filter.conditions ["breast cancer"].
9. "What is the median age of participants in statin trials?" -> analysis unsupported, unsupported_reason explaining that participant ages are not available through this backend.
"""


def _structured_summary(structured: EntityFilter) -> str:
    data = {k: v for k, v in structured.model_dump(mode="json").items() if v not in (None, [], "")}
    return json.dumps(data) if data else "none"


class AnthropicPlanner:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        timeout_seconds: float = 45.0,
        max_retries: int = 2,
        client: anthropic.AsyncAnthropic | None = None,
    ):
        self.model = model
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=max_retries)

    async def draft(self, query: str, structured: EntityFilter, *, today: date, feedback: str | None = None) -> PlanDraft:
        """One model call. `feedback` carries the validation error of a previous attempt."""
        last5 = today.year - 5
        system = SYSTEM_PROMPT.format(today=today.isoformat(), last5=last5)
        user = (
            f"Question: {query.strip()}\n"
            f"Structured fields supplied with the request: {_structured_summary(structured)}\n"
            f"Call {TOOL_NAME} now."
        )
        if feedback:
            user += f"\n\nYour previous plan was rejected: {feedback}\nCall {TOOL_NAME} again with a corrected plan."
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[
                    {
                        "name": TOOL_NAME,
                        "description": "Submit the structured query plan for the question.",
                        "input_schema": PlanDraft.model_json_schema(),
                    }
                ],
                tool_choice={"type": "auto"},
            )
        except anthropic.AuthenticationError as exc:
            raise PlannerUnavailable("Anthropic API key was rejected") from exc
        except anthropic.APIError as exc:
            raise PlannerUnavailable(f"Anthropic API error: {exc}") from exc

        if response.stop_reason == "refusal":
            raise PlannerUnavailable("the model declined to plan this question")
        if response.stop_reason == "max_tokens":
            raise PlannerBadOutput("the plan was cut off before it was complete; keep it shorter")
        tool_input = next((block.input for block in response.content if block.type == "tool_use"), None)
        if not isinstance(tool_input, dict):
            raise PlannerBadOutput("no plan was submitted; call the submit_query_plan tool")
        try:
            draft = PlanDraft.model_validate(_coerce_case(tool_input))
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:6])
            raise PlannerBadOutput(f"the plan did not match the schema ({problems})") from exc
        if draft.analysis == AnalysisType.UNSUPPORTED and not draft.unsupported_reason:
            draft.unsupported_reason = "The question does not map to a supported analysis."
        return draft
