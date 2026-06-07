from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportArtifact(ReportModel):
    run_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    task_summary: dict[str, Any]
    model_summary: dict[str, Any]
    coverage_summary: dict[str, Any]
    score_summary: dict[str, Any]
    severity_caps: list[dict[str, Any]]
    episode_summaries: list[dict[str, Any]]
    failed_items: list[dict[str, Any]]
    evidence_index: dict[str, Any]
    improvement_suggestions: list[dict[str, Any]]
    risk_guard_summary: dict[str, Any] = Field(default_factory=dict)


class BadcaseItem(ReportModel):
    id: str
    run_id: str
    episode_id: str
    task_id: str
    scenario_id: str
    failure_type: str
    severity: str
    requirement_ids: list[str]
    evidence_turn_ids: list[str]
    summary: str
    replay_config: dict[str, Any]


class GoldenCase(ReportModel):
    id: str
    task_id: str
    scenario_id: str
    episode_id: str | None = None
    description: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GoldenLabel(ReportModel):
    id: str
    golden_case_id: str
    requirement_id: str
    expected_verdict: str
    labeler: str = "human"
    rationale: str = ""

