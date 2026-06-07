from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import Severity, Verdict


class ScoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class ScoreItem(ScoreModel):
    id: str
    run_id: str
    episode_id: str
    rubric_item_id: str
    requirement_id: str
    weight: float
    verdict: Verdict
    raw_score: float
    weighted_score: float
    evidence_judge_ids: list[str] = Field(default_factory=list)


class SeverityCap(ScoreModel):
    reason: str
    cap_score: float
    severity: Severity
    judge_event_id: str
    risk_category_id: str | None = None
    source_cap_id: str | None = None
    forbidden_behavior_id: str | None = None


class ScoreSummary(ScoreModel):
    run_id: str
    episode_id: str | None = None
    total_score: float
    possible_score: float
    normalized_score: float
    caps_applied: list[SeverityCap] = Field(default_factory=list)
    per_dimension: dict[str, float] = Field(default_factory=dict)
    items: list[ScoreItem] = Field(default_factory=list)
