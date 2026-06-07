from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from outbound_eval.domain.enums import CheckMethod, RequirementCategory, RiskGuardType, ScenarioType, Severity
from outbound_eval.domain.schemas_markdown import SourceRef


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class TaskVariable(DomainModel):
    name: str
    kind: str = "unknown"
    examples: list[str] = Field(default_factory=list)
    source_text: str = ""


class RequirementItem(DomainModel):
    id: str
    name: str
    category: RequirementCategory
    source_section: str
    source_text: str
    check_method: CheckMethod
    severity: Severity = Severity.MAJOR
    stable_uuid: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def stable_semantic_id(cls, value: str) -> str:
        if not value.startswith("req."):
            raise ValueError("requirement id must start with req.")
        return value

    @field_validator("source_text")
    @classmethod
    def source_text_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("RequirementItem.source_text is required")
        return value


class FlowNode(DomainModel):
    id: str
    name: str
    instruction: str
    requirement_ids: list[str] = Field(default_factory=list)
    order: int = 0


class FlowEdge(DomainModel):
    id: str
    source_node_id: str
    target_node_id: str
    condition: str | None = None


class BranchRule(DomainModel):
    id: str
    name: str
    condition: str
    expected_target_node_id: str | None = None
    requirement_id: str | None = None
    source_text: str


class FAQFact(DomainModel):
    id: str
    question: str
    answer: str
    grounding_source: str
    requirement_ids: list[str] = Field(default_factory=list)

    @field_validator("grounding_source")
    @classmethod
    def grounding_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("FAQFact.grounding_source is required")
        return value


class KnowledgeFact(DomainModel):
    id: str
    text: str
    fact_type: Literal[
        "faq",
        "policy",
        "business_rule",
        "procedure",
        "definition",
        "constraint_detail",
        "other",
    ] = "other"
    source_node_id: str = ""
    source_text: str = ""
    requirement_ids: list[str] = Field(default_factory=list)
    question_patterns: list[str] = Field(default_factory=list)
    answer: str | None = None

    @field_validator("text")
    @classmethod
    def text_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("KnowledgeFact.text is required")
        return value


class ConstraintRule(DomainModel):
    id: str
    name: str
    rule_text: str
    requirement_id: str | None = None
    severity: Severity = Severity.MAJOR


class ForbiddenBehavior(DomainModel):
    id: str
    name: str
    description: str
    severity: Severity = Severity.CRITICAL
    cap_score: float | None = 60.0
    source_text: str


class TerminationRule(DomainModel):
    id: str
    name: str
    condition: str
    source_text: str
    requirement_id: str | None = None


class RubricItem(DomainModel):
    rubric_id: str
    dimension: str
    weight: float = 1.0
    linked_requirement_ids: list[str]
    success_criteria: str
    partial_criteria: str = ""
    fail_criteria: str = ""

    @field_validator("rubric_id")
    @classmethod
    def rubric_id_prefix(cls, value: str) -> str:
        if not value.startswith("rubric."):
            raise ValueError("rubric_id must start with rubric.")
        return value


class RiskCategory(DomainModel):
    id: str
    name: str
    terms: list[str]
    semantic_description: str
    required_guards: list[RiskGuardType]
    default_severity: Severity = Severity.MAJOR
    default_cap: float | None = None
    required_scenario_types: list[ScenarioType] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    guard_hints: dict[str, list[str]] = Field(default_factory=dict)


class DetectedRisk(DomainModel):
    risk_category_id: str
    matched_terms: list[str] = Field(default_factory=list)
    matched_requirement_ids: list[str] = Field(default_factory=list)
    matched_faq_fact_ids: list[str] = Field(default_factory=list)
    matched_constraint_ids: list[str] = Field(default_factory=list)
    source_spans: list[str] = Field(default_factory=list)
    confidence: float = 1.0


class RiskGuardStatus(DomainModel):
    risk_category_id: str
    present_guards: list[RiskGuardType] = Field(default_factory=list)
    missing_guards: list[RiskGuardType] = Field(default_factory=list)
    linked_requirement_ids: list[str] = Field(default_factory=list)
    linked_faq_fact_ids: list[str] = Field(default_factory=list)
    linked_forbidden_behavior_ids: list[str] = Field(default_factory=list)
    linked_rubric_ids: list[str] = Field(default_factory=list)
    linked_severity_cap_ids: list[str] = Field(default_factory=list)
    is_guarded: bool


class SeverityCap(DomainModel):
    id: str
    risk_category_id: str
    condition: str
    cap_score: float
    linked_forbidden_behavior_ids: list[str] = Field(default_factory=list)
    source_text: str

    @field_validator("id")
    @classmethod
    def cap_id_prefix(cls, value: str) -> str:
        if not value.startswith("cap."):
            raise ValueError("severity cap id must start with cap.")
        return value


class RiskCoverageRequirement(DomainModel):
    id: str
    risk_category_id: str
    required_scenario_types: list[ScenarioType]
    linked_requirement_ids: list[str] = Field(default_factory=list)
    min_scenarios: int = 1
    priority: Severity = Severity.MAJOR
    source_finding_id: str | None = None
    rationale: str = ""

    @field_validator("id")
    @classmethod
    def riskcov_id_prefix(cls, value: str) -> str:
        if not value.startswith("riskcov."):
            raise ValueError("risk coverage requirement id must start with riskcov.")
        return value

    @field_validator("linked_requirement_ids")
    @classmethod
    def linked_requirements_required(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("rubric item must link at least one requirement")
        return value


class TaskSpec(DomainModel):
    task_id: str
    task_name: str
    version: str = "1.0"
    role: str
    objective: str
    opening_line: str = ""
    requirements: list[RequirementItem]
    flow_nodes: list[FlowNode] = Field(default_factory=list)
    flow_edges: list[FlowEdge] = Field(default_factory=list)
    branch_rules: list[BranchRule] = Field(default_factory=list)
    knowledge_facts: list[KnowledgeFact] = Field(default_factory=list)
    faq_facts: list[FAQFact] = Field(default_factory=list)
    constraints: list[ConstraintRule] = Field(default_factory=list)
    forbidden_behaviors: list[ForbiddenBehavior] = Field(default_factory=list)
    termination_rules: list[TerminationRule] = Field(default_factory=list)
    rubric: list[RubricItem]
    variables: list[TaskVariable] = Field(default_factory=list)
    detected_risks: list[DetectedRisk] = Field(default_factory=list)
    risk_guard_statuses: list[RiskGuardStatus] = Field(default_factory=list)
    risk_coverage_requirements: list[RiskCoverageRequirement] = Field(default_factory=list)
    severity_caps: list[SeverityCap] = Field(default_factory=list)
    source_sections: dict[str, str] = Field(default_factory=dict)
    source_map: dict[str, SourceRef] = Field(default_factory=dict)
    source_text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    compiled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_links(self) -> "TaskSpec":
        required = {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "version": self.version,
            "role": self.role,
            "objective": self.objective,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"TaskSpec missing required fields: {', '.join(missing)}")
        if not self.requirements:
            raise ValueError("TaskSpec.requirements is required")
        if not self.rubric:
            raise ValueError("TaskSpec.rubric is required")
        req_ids = [req.id for req in self.requirements]
        duplicates = {rid for rid in req_ids if req_ids.count(rid) > 1}
        if duplicates:
            raise ValueError(f"duplicate requirement ids: {sorted(duplicates)}")
        req_id_set = set(req_ids)
        for item in self.rubric:
            missing_links = [rid for rid in item.linked_requirement_ids if rid not in req_id_set]
            if missing_links:
                raise ValueError(f"{item.rubric_id} links unknown requirements: {missing_links}")
        for fact in self.knowledge_facts:
            unknown = [rid for rid in fact.requirement_ids if rid not in req_id_set]
            if unknown:
                raise ValueError(f"{fact.id} links unknown requirements: {unknown}")
        for node in self.flow_nodes:
            unknown = [rid for rid in node.requirement_ids if rid not in req_id_set]
            if unknown:
                raise ValueError(f"{node.id} links unknown requirements: {unknown}")
        for risk_req in self.risk_coverage_requirements:
            unknown = [rid for rid in risk_req.linked_requirement_ids if rid not in req_id_set]
            if unknown:
                raise ValueError(f"{risk_req.id} links unknown requirements: {unknown}")
        return self
