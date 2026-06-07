from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import Severity
from outbound_eval.domain.schemas_markdown import SourceRef
from outbound_eval.domain.schemas_task import KnowledgeFact


class UndModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JudgeDimension(str):
    TASK_COMPLETION = "task_completion"
    FLOW_FOLLOWING = "flow_following"
    KNOWLEDGE_CORRECTNESS = "knowledge_correctness"
    CONSTRAINT_FOLLOWING = "constraint_following"
    EXCEPTION_HANDLING = "exception_handling"
    USER_EXPERIENCE = "user_experience"
    SAFETY_COMPLIANCE = "safety_compliance"


JUDGE_DIMENSIONS = [
    "task_completion",
    "flow_following",
    "knowledge_correctness",
    "constraint_following",
    "exception_handling",
    "user_experience",
    "safety_compliance",
]


class JudgePoint(UndModel):
    id: str
    dimension: Literal[
        "task_completion",
        "flow_following",
        "knowledge_correctness",
        "constraint_following",
        "exception_handling",
        "user_experience",
        "safety_compliance",
    ]
    criterion: str
    pass_criteria: str
    partial_criteria: str = ""
    fail_criteria: str
    evidence_required: bool = True
    weight: float = 1.0
    severity: Severity = Severity.MAJOR
    source_node_id: str = ""
    source_text: str = ""
    linked_requirement_ids: list[str] = Field(default_factory=list)
    linked_knowledge_fact_ids: list[str] = Field(default_factory=list)
    evaluator: Literal["rule", "llm", "hybrid"] = "llm"


class CriticalFailureRule(UndModel):
    id: str
    description: str
    judge_point_id: str
    cap_score: float = 0.0


class AggregationPolicy(UndModel):
    method: Literal["weighted_average", "min_per_dimension"] = "weighted_average"
    dimension_weights: dict[str, float] = Field(default_factory=dict)


class JudgePlan(UndModel):
    task_id: str
    judge_points: list[JudgePoint] = Field(default_factory=list)
    dimension_weights: dict[str, float] = Field(default_factory=dict)
    critical_failure_rules: list[CriticalFailureRule] = Field(default_factory=list)
    aggregation_policy: AggregationPolicy = Field(default_factory=AggregationPolicy)


class DetectedRiskPlan(UndModel):
    risk_category_id: str
    description: str
    severity: Severity = Severity.MAJOR
    auto_guarded: bool = False
    guard_description: str = ""


class RiskCoverageReq(UndModel):
    id: str
    description: str
    linked_risk_category_id: str = ""
    min_scenarios: int = 1
    priority: Severity = Severity.MAJOR


class RiskPlan(UndModel):
    task_id: str
    detected_risks: list[DetectedRiskPlan] = Field(default_factory=list)
    coverage_requirements: list[RiskCoverageReq] = Field(default_factory=list)


class CompileFinding(UndModel):
    code: str
    message: str
    severity: Severity = Severity.MINOR
    blocking: bool = False
    source_node_id: str = ""
    suggestion: str = ""


class TaskUnderstanding(UndModel):
    """LLMTaskCompiler 的核心输出，三类 LLM 的统一理解源。"""

    task_spec: dict[str, Any]
    """TaskSpec 的 model_dump，避免循环引入"""

    judge_plan: JudgePlan
    risk_plan: RiskPlan
    source_map: dict[str, SourceRef] = Field(default_factory=dict)
    compiler_notes: list[str] = Field(default_factory=list)
    compile_findings: list[CompileFinding] = Field(default_factory=list)
    knowledge_facts: list[KnowledgeFact] = Field(default_factory=list)
    raw_instruction: str = ""


class ScenarioPlanItem(UndModel):
    id: str
    title: str
    scenario_type: Literal[
        "main_flow",
        "branch",
        "knowledge_probe",
        "constraint_probe",
        "exception",
        "adversarial",
        "metamorphic",
    ] = "main_flow"
    coverage_intent: str
    linked_judge_point_ids: list[str] = Field(default_factory=list)
    linked_requirement_ids: list[str] = Field(default_factory=list)
    linked_risk_coverage_ids: list[str] = Field(default_factory=list)
    persona_focus: str = ""
    priority: Severity = Severity.MAJOR


class PersonaSpec(UndModel):
    identity: str = ""
    relationship_to_task: str = ""
    motivation: str = ""
    attitude: str = ""
    communication_style: str = ""
    initial_focus: str = ""
    decision_rule: str = ""
    inconvenience_context: str = ""


class ScenarioSpec(UndModel):
    """LLM 构建的完整场景规格。"""

    scenario_id: str
    task_id: str
    title: str
    scenario_type: str = "main_flow"
    persona: PersonaSpec = Field(default_factory=PersonaSpec)
    user_goal: str
    hidden_user_goal: str
    initial_user_utterance: str
    dialogue_direction: list[str] = Field(default_factory=list)
    expected_model_behavior: list[str] = Field(default_factory=list)
    forbidden_behavior: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    linked_judge_point_ids: list[str] = Field(default_factory=list)
    covered_requirement_ids: list[str] = Field(default_factory=list)
    max_turns: int = 10
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScenarioPlan(UndModel):
    task_id: str
    items: list[ScenarioPlanItem] = Field(default_factory=list)


class ScenarioSet(UndModel):
    task_id: str
    scenarios: list[ScenarioSpec] = Field(default_factory=list)


class UserSimulatorOutput(UndModel):
    utterance: str
    intent: str = ""
    state: str = "active"
    memory_update: str = ""
    should_continue: bool = True
    covered_judge_point_ids: list[str] = Field(default_factory=list)
    stop_reason: str | None = None


class JudgePointResult(UndModel):
    judge_point_id: str
    verdict: Literal["pass", "partial", "fail", "not_applicable"] = "not_applicable"
    score: float = 0.0
    evidence_turn_ids: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list)
    reason: str = ""
    confidence: float = 0.8
    suggested_fix: str = ""


class SemanticJudgeResult(UndModel):
    scenario_id: str
    episode_id: str
    overall_summary: str = ""
    item_results: list[JudgePointResult] = Field(default_factory=list)
    critical_failures: list[str] = Field(default_factory=list)
    total_score: float = 0.0
