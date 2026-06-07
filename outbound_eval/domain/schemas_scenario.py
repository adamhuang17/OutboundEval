from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from outbound_eval.domain.enums import ScenarioType, Severity


class ScenarioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class PersonaSpec(ScenarioModel):
    persona_id: str
    role: str
    age_range: str
    gender: str = "unknown"
    attitude: str
    knowledge_level: str
    speaking_style: str
    common_working_hours: str
    working_location: str


class HiddenState(ScenarioModel):
    hidden_goal: str
    facts_known_by_user: list[str] = Field(default_factory=list)
    facts_not_to_leak: list[str] = Field(default_factory=list)


class TriggerPlan(ScenarioModel):
    intent: str
    steps: list[str]
    required_user_actions: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)


class ExpectedBehavior(ScenarioModel):
    id: str
    requirement_id: str
    description: str
    severity: Severity = Severity.MAJOR


class ScenarioSpec(ScenarioModel):
    scenario_id: str
    task_id: str
    scenario_name: str
    scenario_type: ScenarioType
    persona: PersonaSpec
    user_prior_conditions: list[str]
    hidden_goal: str
    trigger_plan: TriggerPlan
    covered_requirement_ids: list[str]
    expected_behavior_ids: list[str]
    max_turns: int = 10
    difficulty: str = "medium"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("covered_requirement_ids")
    @classmethod
    def coverage_required(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("ScenarioSpec must cover at least one requirement")
        return value


class CoverageMatrix(ScenarioModel):
    task_id: str
    scenarios: list[ScenarioSpec]
    requirement_coverage: dict[str, list[str]]
    flow_node_coverage: dict[str, list[str]]
    branch_coverage: dict[str, list[str]]
    faq_coverage: dict[str, list[str]]
    risk_coverage: dict[str, list[str]]
    risk_requirement_coverage: dict[str, list[str]] = Field(default_factory=dict)
    uncovered_requirement_ids: list[str]
    uncovered_risk_coverage_requirement_ids: list[str] = Field(default_factory=list)

    @property
    def requirement_coverage_rate(self) -> float:
        total = len(self.requirement_coverage) + len(self.uncovered_requirement_ids)
        if total == 0:
            return 1.0
        return len(self.requirement_coverage) / total
