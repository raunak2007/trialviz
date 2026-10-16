"""Planner orchestration: model first, one corrective retry, rules as the fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trialviz.interpret.llm import AnthropicPlanner, PlannerBadOutput, PlannerUnavailable
from trialviz.interpret.normalize import PlanError, normalize
from trialviz.interpret.rules import rule_plan
from trialviz.schemas.plan import EntityFilter, PlanDraft, QueryPlan
from trialviz.schemas.response import PlannerInfo

log = logging.getLogger(__name__)

PlannerMode = Literal["auto", "llm", "rules"]


@dataclass
class PlanOutcome:
    plan: QueryPlan
    info: PlannerInfo
    draft: PlanDraft


class Planner:
    def __init__(self, llm: AnthropicPlanner | None):
        self.llm = llm

    async def plan(
        self, query: str, structured: EntityFilter, *, mode: PlannerMode = "auto", today: date | None = None
    ) -> PlanOutcome:
        """Produce an executable plan or raise PlanError for the client.

        Model path: draft -> normalize. If the draft fails schema validation or normalization rejects
        it, the model gets the error once and tries again. If the model is unavailable (no key, API
        failure) or fails twice, and the mode allows it, the rule-based parser takes over and
        `info.fallback_reason` says why.
        """
        today = today or date.today()
        use_llm = mode in ("auto", "llm") and self.llm is not None
        if mode == "llm" and self.llm is None:
            raise PlanError(
                "needs_clarification",
                "The language-model planner is not configured (set ANTHROPIC_API_KEY) but planner='llm' was requested.",
            )

        fallback_reason: str | None = None
        if use_llm:
            assert self.llm is not None
            feedback: str | None = None
            last_error: PlanError | None = None
            for attempt in (1, 2):
                try:
                    draft = await self.llm.draft(query, structured, today=today, feedback=feedback)
                except PlannerUnavailable as exc:
                    fallback_reason = str(exc)
                    log.warning("model planner unavailable: %s", exc)
                    break
                except PlannerBadOutput as exc:
                    log.warning("model plan invalid (attempt %d): %s", attempt, exc)
                    if attempt == 1:
                        feedback = str(exc)
                        continue
                    fallback_reason = f"model output was invalid twice: {exc}"
                    break
                try:
                    plan = normalize(draft, structured, mode="llm", today=today)
                    return PlanOutcome(
                        plan=plan, info=PlannerInfo(mode="llm", model=self.llm.model, attempts=attempt), draft=draft
                    )
                except PlanError as exc:
                    last_error = exc
                    if exc.code == "unsupported_query" or attempt == 2:
                        raise
                    feedback = exc.message
            if last_error is not None and fallback_reason is None:
                raise last_error
            if mode == "llm":
                raise PlanError("needs_clarification", f"The language-model planner failed: {fallback_reason}")
        elif mode == "auto":
            fallback_reason = "no Anthropic API key configured"

        draft = rule_plan(query, structured, today=today)
        plan = normalize(draft, structured, mode="rules", today=today)
        return PlanOutcome(
            plan=plan, info=PlannerInfo(mode="rules", model=None, attempts=1, fallback_reason=fallback_reason), draft=draft
        )
