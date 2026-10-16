from datetime import date

import pytest

from trialviz.interpret.llm import AnthropicPlanner, PlannerUnavailable
from trialviz.interpret.normalize import PlanError, normalize
from trialviz.interpret.planner import Planner
from trialviz.interpret.rules import rule_plan
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
    VizType,
)
from trialviz.schemas.plan import EntityFilter, PlanDraft, Series

TODAY = date(2026, 10, 15)


def plan_for(query: str, structured: EntityFilter | None = None):
    structured = structured or EntityFilter()
    return normalize(rule_plan(query, structured, today=TODAY), structured, mode="rules", today=TODAY)


# ---------------------------------------------------------------- rule-based parser on the appendix queries


def test_time_trend_with_since():
    p = plan_for("How has the number of trials for pembrolizumab changed per year since 2015?")
    assert p.analysis == AnalysisType.TIME_TREND and p.viz_type == VizType.TIME_SERIES
    assert p.filter.terms == ["pembrolizumab"]
    assert (p.filter.start_year, p.filter.end_year) == (2015, 2026)


def test_time_trend_each_year():
    p = plan_for("How many trials started each year for melanoma?")
    assert p.analysis == AnalysisType.TIME_TREND
    assert p.filter.conditions == ["melanoma"]
    assert p.filter.start_year == 2016  # defaulted to the last 10 years
    assert any("last 10 years" in a for a in p.assumptions)


def test_distribution_by_phase_restricts_to_interventional():
    p = plan_for("How are melanoma trials distributed across phases?")
    assert p.analysis == AnalysisType.DISTRIBUTION and p.dimension == Dimension.PHASE
    assert p.filter.study_types == [StudyType.INTERVENTIONAL]
    assert p.viz_type == VizType.BAR_CHART


def test_intervention_types():
    p = plan_for("What are the most common intervention types for diabetes trials?")
    assert p.dimension == Dimension.INTERVENTION_TYPE and p.filter.conditions == ["diabetes"]


def test_comparison_of_drugs():
    p = plan_for("Compare phases for trials involving nivolumab vs ipilimumab.")
    assert p.analysis == AnalysisType.COMPARISON and p.dimension == Dimension.PHASE
    assert [s.label for s in p.series] == ["nivolumab", "ipilimumab"]
    assert p.viz_type == VizType.GROUPED_BAR_CHART
    resolved = dict(p.resolved_filters())
    assert resolved["nivolumab"].terms == ["nivolumab"] and resolved["nivolumab"].study_types == [StudyType.INTERVENTIONAL]


def test_comparison_of_conditions_by_sponsor_class():
    p = plan_for("Compare sponsor categories across two conditions: asthma vs COPD")
    assert p.dimension == Dimension.SPONSOR_CLASS
    assert [s.filter.conditions for s in p.series] == [["asthma"], ["COPD"]]


def test_geographic_with_status():
    p = plan_for("Which countries have the most recruiting trials for type 2 diabetes?")
    assert p.analysis == AnalysisType.GEOGRAPHIC and p.dimension == Dimension.COUNTRY
    assert p.filter.statuses == [OverallStatus.RECRUITING]
    assert p.filter.conditions == ["type 2 diabetes"]
    assert p.top_n == 20


def test_networks():
    p = plan_for("Show a network of sponsors and drugs for melanoma trials.")
    assert p.analysis == AnalysisType.NETWORK and p.network == NetworkKind.SPONSOR_DRUG
    assert p.filter.conditions == ["melanoma"]
    p = plan_for("Which drugs frequently co-occur in combination studies for lung cancer?")
    assert p.network == NetworkKind.DRUG_DRUG and p.filter.conditions == ["lung cancer"]


def test_scatter_and_histogram():
    p = plan_for("Plot enrollment against start year for breast cancer trials")
    assert p.analysis == AnalysisType.SCATTER
    assert (p.numeric_x, p.numeric_y) == (NumericField.START_YEAR, NumericField.ENROLLMENT)
    p = plan_for("Histogram of enrollment for asthma trials")
    assert p.analysis == AnalysisType.HISTOGRAM and p.numeric_x == NumericField.ENROLLMENT


def test_structured_fields_win_and_this_drug_resolves():
    p = plan_for("How has the number of trials for this drug changed over time?", EntityFilter(drugs=["Pembrolizumab"]))
    assert p.filter.drugs == ["Pembrolizumab"] and p.filter.terms == []
    p = plan_for("trials for aspirin by phase", EntityFilter(drugs=["Metformin"], phases=[Phase.PHASE3]))
    assert p.filter.drugs == ["Metformin"] and p.filter.phases == [Phase.PHASE3]


def test_phase_number_is_a_filter_not_a_dimension():
    p = plan_for("Which sponsors run the most phase 3 trials for melanoma?")
    assert p.dimension == Dimension.SPONSOR and p.filter.phases == [Phase.PHASE3]


def test_quarterly_granularity_and_year_range():
    p = plan_for("Show trials per quarter for semaglutide between 2018 and 2024")
    assert p.time_granularity == TimeGranularity.QUARTER
    assert (p.filter.start_year, p.filter.end_year) == (2018, 2024)
    assert p.filter.terms == ["semaglutide"]


def test_unanswerable_questions_raise_plan_errors():
    with pytest.raises(PlanError) as exc:
        plan_for("Tell me about pembrolizumab")
    assert exc.value.code == "unsupported_query" and exc.value.suggestions
    with pytest.raises(PlanError) as exc:
        plan_for("Trials by phase")
    assert exc.value.code == "needs_clarification"


# ---------------------------------------------------------------- normalize rules on hand-built drafts


def test_normalize_promotes_start_year_distribution_to_trend():
    draft = PlanDraft(
        analysis=AnalysisType.DISTRIBUTION, dimension=Dimension.START_YEAR, filter=EntityFilter(drugs=["x"]), interpretation="i"
    )
    p = normalize(draft, EntityFilter(), mode="llm", today=TODAY)
    assert p.analysis == AnalysisType.TIME_TREND and p.dimension is None


def test_normalize_comparison_defaults_and_rejects_identical_series():
    draft = PlanDraft(
        analysis=AnalysisType.COMPARISON,
        filter=EntityFilter(conditions=["melanoma"]),
        series=[Series(label="A", filter=EntityFilter(drugs=["a"])), Series(label="B", filter=EntityFilter(drugs=["b"]))],
        split_by=Dimension.STATUS,
        interpretation="i",
    )
    p = normalize(draft, EntityFilter(), mode="llm", today=TODAY)
    assert p.dimension == Dimension.PHASE and p.split_by is None
    assert any("Compared the entities by phase" in a for a in p.assumptions)
    same = draft.model_copy(
        update={
            "series": [Series(label="A", filter=EntityFilter(drugs=["a"])), Series(label="B", filter=EntityFilter(drugs=["a"]))]
        }
    )
    with pytest.raises(PlanError):
        normalize(same, EntityFilter(), mode="llm", today=TODAY)


def test_normalize_measure_and_numeric_defaults():
    draft = PlanDraft(
        analysis=AnalysisType.SCATTER, filter=EntityFilter(conditions=["x"]), measure=Measure.TOTAL_ENROLLMENT, interpretation="i"
    )
    p = normalize(draft, EntityFilter(), mode="llm", today=TODAY)
    assert p.measure == Measure.TRIAL_COUNT and p.numeric_x == NumericField.START_YEAR and p.numeric_y == NumericField.ENROLLMENT


def test_normalize_reversed_years_and_unsupported():
    draft = PlanDraft(
        analysis=AnalysisType.DISTRIBUTION,
        dimension=Dimension.PHASE,
        filter=EntityFilter(drugs=["x"], start_year=2020, end_year=2010),
        interpretation="i",
    )
    with pytest.raises(PlanError):
        normalize(draft, EntityFilter(), mode="llm", today=TODAY)
    unsupported = PlanDraft(analysis=AnalysisType.UNSUPPORTED, interpretation="i", unsupported_reason="needs outcomes")
    with pytest.raises(PlanError) as exc:
        normalize(unsupported, EntityFilter(), mode="llm", today=TODAY)
    assert exc.value.code == "unsupported_query" and "outcomes" in exc.value.message


# ---------------------------------------------------------------- planner orchestration with a scripted model


class ScriptedLLM(AnthropicPlanner):
    """Stands in for the model: returns scripted drafts or raises, and records the feedback it was given."""

    def __init__(self, outcomes):
        self.model = "scripted"
        self.outcomes = list(outcomes)
        self.feedback: list[str | None] = []

    async def draft(self, query, structured, *, today, feedback=None):
        self.feedback.append(feedback)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_planner_retries_once_with_feedback():
    bad = PlanDraft(analysis=AnalysisType.COMPARISON, filter=EntityFilter(drugs=["a"]), series=[], interpretation="i")
    good = PlanDraft(
        analysis=AnalysisType.DISTRIBUTION, dimension=Dimension.PHASE, filter=EntityFilter(drugs=["a"]), interpretation="i"
    )
    llm = ScriptedLLM([bad, good])
    outcome = await Planner(llm).plan("q", EntityFilter(), today=TODAY)
    assert outcome.info.mode == "llm" and outcome.info.attempts == 2
    assert llm.feedback[1] and "comparison" in llm.feedback[1]


async def test_planner_falls_back_to_rules_when_model_unavailable():
    llm = ScriptedLLM([PlannerUnavailable("no network")])
    outcome = await Planner(llm).plan("How are melanoma trials distributed across phases?", EntityFilter(), today=TODAY)
    assert outcome.info.mode == "rules" and outcome.info.fallback_reason == "no network"
    assert outcome.plan.dimension == Dimension.PHASE


async def test_planner_llm_mode_requires_model():
    with pytest.raises(PlanError):
        await Planner(None).plan("q", EntityFilter(), mode="llm", today=TODAY)
    outcome = await Planner(None).plan(
        "How are melanoma trials distributed across phases?", EntityFilter(), mode="auto", today=TODAY
    )
    assert outcome.info.fallback_reason == "no Anthropic API key configured"
