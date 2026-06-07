from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from outbound_eval.domain.enums import FindingDecision, FindingSource, Severity, Verdict


class JudgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class SpecFinding(JudgeModel):
    id: str | None = None
    source: FindingSource
    severity: Severity
    requirement_ref: str | None = None
    detail: str
    suggested_fix: str = ""
    decision: FindingDecision = FindingDecision.HUMAN_NEEDED
    dismissed: bool = False
    blocking: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckerStartedEvent(JudgeModel):
    id: str
    run_id: str
    episode_id: str
    checker_name: str
    checker_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JudgeEvent(JudgeModel):
    id: str
    run_id: str
    episode_id: str
    checker_name: str
    checker_version: str
    requirement_id: str | None
    rubric_item_id: str | None
    verdict: Verdict
    confidence: float
    evidence_turn_ids: list[str]
    evidence_quotes: list[str]
    reason: str
    score_delta: float
    severity: Severity
    raw_output: dict[str, Any] | None = None

    @field_validator("confidence")
    @classmethod
    def confidence_range(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("confidence must be between 0 and 1")
        return value

    @field_validator("evidence_turn_ids")
    @classmethod
    def evidence_for_scored_verdicts(cls, value: list[str], info) -> list[str]:
        verdict = info.data.get("verdict")
        if verdict in {Verdict.PASS, Verdict.PARTIAL, Verdict.FAIL, "pass", "partial", "fail"} and not value:
            raise ValueError("pass/partial/fail JudgeEvent must include evidence_turn_ids")
        return value
