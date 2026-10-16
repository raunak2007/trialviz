"""The Anthropic planner with the SDK client stubbed out: prompt and tool assembly, parsing, and error mapping."""

from dataclasses import dataclass, field
from datetime import date

import anthropic
import httpx
import pytest

from trialviz.interpret.llm import SYSTEM_PROMPT, TOOL_NAME, AnthropicPlanner, PlannerBadOutput, PlannerUnavailable
from trialviz.interpret.planner import Planner
from trialviz.schemas.enums import AnalysisType, Dimension, Phase
from trialviz.schemas.plan import EntityFilter

TODAY = date(2026, 10, 15)


@dataclass
class ToolUseBlock:
    input: dict
    type: str = "tool_use"
    name: str = TOOL_NAME


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeResponse:
    content: list = field(default_factory=list)
    stop_reason: str = "tool_use"


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, *outcomes):
        self.messages = FakeMessages(outcomes)


def planner_with(*outcomes) -> tuple[AnthropicPlanner, FakeClient]:
    client = FakeClient(*outcomes)
    return AnthropicPlanner(model="test-model", client=client), client  # type: ignore[arg-type]


GOOD_INPUT = {
    "analysis": "distribution",
    "dimension": "phase",
    "filter": {"conditions": ["melanoma"]},
    "interpretation": "Count melanoma trials by phase.",
}


async def test_tool_call_carries_schema_prompt_and_fields():
    planner, client = planner_with(FakeResponse([ToolUseBlock(GOOD_INPUT)]))
    draft = await planner.draft(
        "How are melanoma trials distributed across phases?", EntityFilter(conditions=["melanoma"]), today=TODAY
    )
    assert draft.analysis == AnalysisType.DISTRIBUTION and draft.dimension == Dimension.PHASE
    call = client.messages.calls[0]
    assert call["model"] == "test-model"
    assert call["tool_choice"] == {"type": "auto"}  # forced tool choice is a 400 on the current models
    assert f"Call {TOOL_NAME} now." in call["messages"][0]["content"]
    assert call["tools"][0]["name"] == TOOL_NAME and "properties" in call["tools"][0]["input_schema"]
    assert "2026-10-15" in call["system"] and 'last 5 years" means start_year = 2021' in call["system"]
    user = call["messages"][0]["content"]
    assert "How are melanoma trials distributed across phases?" in user
    assert '"conditions": ["melanoma"]' in user


async def test_feedback_is_appended_on_retry():
    planner, client = planner_with(FakeResponse([ToolUseBlock(GOOD_INPUT)]))
    await planner.draft("q", EntityFilter(), today=TODAY, feedback="a comparison needs at least two series")
    assert "a comparison needs at least two series" in client.messages.calls[0]["messages"][0]["content"]


async def test_enum_casing_is_coerced():
    sloppy = {
        **GOOD_INPUT,
        "analysis": "Distribution",
        "dimension": "PHASE",
        "filter": {"conditions": ["x"], "phases": ["phase 3"]},
    }
    planner, _ = planner_with(FakeResponse([ToolUseBlock(sloppy)]))
    draft = await planner.draft("q", EntityFilter(), today=TODAY)
    assert draft.analysis == AnalysisType.DISTRIBUTION and draft.filter.phases == [Phase.PHASE3]


async def test_invalid_plans_are_bad_output_not_unavailable():
    planner, _ = planner_with(FakeResponse([ToolUseBlock({"analysis": "bogus", "interpretation": "i"})]))
    with pytest.raises(PlannerBadOutput, match="analysis"):
        await planner.draft("q", EntityFilter(), today=TODAY)
    planner, _ = planner_with(FakeResponse([TextBlock("I cannot")], stop_reason="end_turn"))
    with pytest.raises(PlannerBadOutput, match="submit_query_plan"):
        await planner.draft("q", EntityFilter(), today=TODAY)
    planner, _ = planner_with(FakeResponse([ToolUseBlock(GOOD_INPUT)], stop_reason="max_tokens"))
    with pytest.raises(PlannerBadOutput, match="cut off"):
        await planner.draft("q", EntityFilter(), today=TODAY)


async def test_refusals_and_api_errors_are_unavailable():
    planner, _ = planner_with(FakeResponse([], stop_reason="refusal"))
    with pytest.raises(PlannerUnavailable, match="declined"):
        await planner.draft("q", EntityFilter(), today=TODAY)
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    planner, _ = planner_with(anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None))
    with pytest.raises(PlannerUnavailable, match="rejected"):
        await planner.draft("q", EntityFilter(), today=TODAY)
    planner, _ = planner_with(anthropic.APIConnectionError(request=request))
    with pytest.raises(PlannerUnavailable, match="API error"):
        await planner.draft("q", EntityFilter(), today=TODAY)


async def test_planner_retries_bad_output_once_then_falls_back():
    bad = FakeResponse([ToolUseBlock({"analysis": "bogus", "interpretation": "i"})])
    good = FakeResponse([ToolUseBlock(GOOD_INPUT)])
    llm, client = planner_with(bad, good)
    outcome = await Planner(llm).plan("How are melanoma trials distributed across phases?", EntityFilter(), today=TODAY)
    assert outcome.info.mode == "llm" and outcome.info.attempts == 2
    assert "did not match the schema" in client.messages.calls[1]["messages"][0]["content"]

    llm, _ = planner_with(bad, bad)
    outcome = await Planner(llm).plan("How are melanoma trials distributed across phases?", EntityFilter(), today=TODAY)
    assert outcome.info.mode == "rules" and "invalid twice" in (outcome.info.fallback_reason or "")


def test_system_prompt_formats_cleanly():
    text = SYSTEM_PROMPT.format(today="2026-10-15", last5=2021)
    assert "Today's date is 2026-10-15" in text
    assert "{today}" not in text and "{last5}" not in text
    assert "analysis unsupported" in text
