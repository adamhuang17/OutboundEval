"""LLMTaskCompiler — 用 LLM 把任意 Markdown 任务说明编译成 TaskUnderstanding。

替代 RuleBasedSpecExtractor 的主路径。
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.compiler.markdown_ast import MarkdownAstParser
from outbound_eval.compiler.task_compiler_prompts import build_compiler_messages
from outbound_eval.domain.schemas_markdown import MarkdownAst, SourceRef
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_understanding import (
    CompileFinding,
    JudgePlan,
    JudgePoint,
    KnowledgeFact,
    RiskPlan,
    TaskUnderstanding,
    DetectedRiskPlan,
    RiskCoverageReq,
    AggregationPolicy,
)
from outbound_eval.llm.structured_client import StructuredLLMClient
from outbound_eval.domain.enums import Severity


class _LLMCompilerDraft(BaseModel):
    """LLM 输出的草稿，允许 extra 字段。"""

    model_config = ConfigDict(extra="allow")

    task_name: str = "未命名任务"
    role: str = ""
    objective: str = ""
    opening_line: str = ""
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    flow_nodes: list[dict[str, Any]] = Field(default_factory=list)
    branch_rules: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_facts: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_behaviors: list[dict[str, Any]] = Field(default_factory=list)
    termination_rules: list[dict[str, Any]] = Field(default_factory=list)
    variables: list[dict[str, Any]] = Field(default_factory=list)
    judge_plan: dict[str, Any] = Field(default_factory=dict)
    risk_plan: dict[str, Any] = Field(default_factory=dict)
    compile_findings: list[dict[str, Any]] = Field(default_factory=list)
    compiler_notes: list[str] = Field(default_factory=list)


def _normalize_id(value: str, prefix: str, seq_map: dict[str, int]) -> str:
    """确保 id 有正确 prefix，不会重复。"""
    if value and value.startswith(prefix):
        return value
    seq_map[prefix] = seq_map.get(prefix, 0) + 1
    return f"{prefix}{seq_map[prefix]:03d}"


def _to_severity(value: str) -> str:
    mapping = {"critical": "critical", "major": "major", "minor": "minor"}
    return mapping.get(str(value).lower(), "major")


def _draft_to_task_spec(draft: _LLMCompilerDraft, task_id: str) -> dict[str, Any]:
    """把 LLM draft 转成 TaskSpec-compatible dict。"""
    seq: dict[str, int] = {}

    requirements = []
    for r in draft.requirements:
        rid = _normalize_id(r.get("id", ""), "req.", seq)
        requirements.append(
            {
                "id": rid,
                "name": r.get("name", "未命名需求"),
                "category": r.get("category", "task"),
                "source_section": r.get("source_node_id", ""),
                "source_text": r.get("source_text", ""),
                "check_method": r.get("check_method", "llm"),
                "severity": _to_severity(r.get("severity", "major")),
                "tags": r.get("tags", []),
            }
        )

    flow_nodes = []
    for i, fn in enumerate(draft.flow_nodes):
        flow_nodes.append(
            {
                "id": fn.get("id", f"flow.{i+1:03d}"),
                "name": fn.get("name", f"步骤{i+1}"),
                "instruction": fn.get("instruction", ""),
                "requirement_ids": fn.get("requirement_ids", []),
                "order": fn.get("order", i),
            }
        )

    branch_rules = []
    for b in draft.branch_rules:
        branch_rules.append(
            {
                "id": _normalize_id(b.get("id", ""), "branch.", seq),
                "name": b.get("name", ""),
                "condition": b.get("condition", ""),
                "source_text": b.get("source_text", ""),
            }
        )

    constraints = []
    for c in draft.constraints:
        constraints.append(
            {
                "id": _normalize_id(c.get("id", ""), "con.", seq),
                "name": c.get("name", ""),
                "rule_text": c.get("rule_text", ""),
                "severity": _to_severity(c.get("severity", "major")),
            }
        )

    forbidden_behaviors = []
    for fb in draft.forbidden_behaviors:
        forbidden_behaviors.append(
            {
                "id": _normalize_id(fb.get("id", ""), "fb.", seq),
                "name": fb.get("name", ""),
                "description": fb.get("description", ""),
                "severity": _to_severity(fb.get("severity", "critical")),
                "cap_score": float(fb.get("cap_score", 60.0)),
                "source_text": fb.get("source_text", ""),
            }
        )

    termination_rules = []
    for t in draft.termination_rules:
        termination_rules.append(
            {
                "id": _normalize_id(t.get("id", ""), "term.", seq),
                "name": t.get("name", ""),
                "condition": t.get("condition", ""),
                "source_text": t.get("source_text", ""),
            }
        )

    variables = []
    for v in draft.variables:
        variables.append(
            {
                "name": v.get("name", ""),
                "kind": v.get("kind", "unknown"),
                "examples": v.get("examples", []),
                "source_text": v.get("source_text", ""),
            }
        )

    # Build minimal rubric for backwards compat (at least one item)
    rubric = []
    req_ids = [r["id"] for r in requirements]
    if req_ids:
        rubric.append(
            {
                "rubric_id": "rubric.001",
                "dimension": "overall",
                "weight": 1.0,
                "linked_requirement_ids": req_ids[:3],
                "success_criteria": "完成任务目标并遵守全部约束",
                "fail_criteria": "未完成任务或违反关键约束",
            }
        )

    # faq_facts backwards compat
    faq_facts = []
    for kf in draft.knowledge_facts:
        if kf.get("fact_type", "faq") == "faq" and kf.get("answer"):
            faq_facts.append(
                {
                    "id": _normalize_id(kf.get("id", ""), "faq.", seq),
                    "question": kf.get("question_patterns", [""])[0] if kf.get("question_patterns") else kf.get("text", ""),
                    "answer": kf.get("answer", ""),
                    "grounding_source": kf.get("source_text", "原文"),
                    "requirement_ids": kf.get("requirement_ids", []),
                }
            )

    return {
        "task_id": task_id,
        "task_name": draft.task_name,
        "role": draft.role,
        "objective": draft.objective,
        "opening_line": draft.opening_line,
        "requirements": requirements,
        "flow_nodes": flow_nodes,
        "flow_edges": [],
        "branch_rules": branch_rules,
        "faq_facts": faq_facts,
        "constraints": constraints,
        "forbidden_behaviors": forbidden_behaviors,
        "termination_rules": termination_rules,
        "variables": variables,
        "rubric": rubric,
        "detected_risks": [],
        "risk_guard_statuses": [],
        "risk_coverage_requirements": [],
        "severity_caps": [],
        "source_text": "",
    }


def _draft_to_judge_plan(draft: _LLMCompilerDraft, task_id: str) -> JudgePlan:
    jp_raw = draft.judge_plan
    seq: dict[str, int] = {}
    judge_points = []
    for jp in jp_raw.get("judge_points", []):
        judge_points.append(
            JudgePoint(
                id=_normalize_id(jp.get("id", ""), "jp.", seq),
                dimension=jp.get("dimension", "task_completion"),
                criterion=jp.get("criterion", ""),
                pass_criteria=jp.get("pass_criteria", ""),
                partial_criteria=jp.get("partial_criteria", ""),
                fail_criteria=jp.get("fail_criteria", ""),
                severity=_to_severity(jp.get("severity", "major")),
                weight=float(jp.get("weight", 1.0)),
                source_node_id=jp.get("source_node_id", ""),
                source_text=jp.get("source_text", ""),
                linked_requirement_ids=jp.get("linked_requirement_ids", []),
                evaluator=jp.get("evaluator", "llm"),
            )
        )
    dimension_weights = jp_raw.get(
        "dimension_weights",
        {
            "task_completion": 0.25,
            "flow_following": 0.2,
            "knowledge_correctness": 0.2,
            "constraint_following": 0.15,
            "exception_handling": 0.1,
            "user_experience": 0.05,
            "safety_compliance": 0.05,
        },
    )
    return JudgePlan(task_id=task_id, judge_points=judge_points, dimension_weights=dimension_weights)


def _draft_to_risk_plan(draft: _LLMCompilerDraft, task_id: str) -> RiskPlan:
    rp_raw = draft.risk_plan
    detected = []
    for r in rp_raw.get("detected_risks", []):
        detected.append(
            DetectedRiskPlan(
                risk_category_id=r.get("risk_category_id", "unknown"),
                description=r.get("description", ""),
                severity=_to_severity(r.get("severity", "major")),
                auto_guarded=bool(r.get("auto_guarded", False)),
                guard_description=r.get("guard_description", ""),
            )
        )
    reqs = []
    for cr in rp_raw.get("coverage_requirements", []):
        reqs.append(
            RiskCoverageReq(
                id=cr.get("id", f"riskcov.{len(reqs)+1:03d}"),
                description=cr.get("description", ""),
                linked_risk_category_id=cr.get("linked_risk_category_id", ""),
                min_scenarios=int(cr.get("min_scenarios", 1)),
                priority=_to_severity(cr.get("priority", "major")),
            )
        )
    return RiskPlan(task_id=task_id, detected_risks=detected, coverage_requirements=reqs)


def _draft_to_knowledge_facts(draft: _LLMCompilerDraft) -> list[KnowledgeFact]:
    facts = []
    for kf in draft.knowledge_facts:
        facts.append(
            KnowledgeFact(
                id=kf.get("id", f"kf.{len(facts)+1:03d}"),
                text=kf.get("text", ""),
                fact_type=kf.get("fact_type", "faq"),
                source_node_id=kf.get("source_node_id", ""),
                source_text=kf.get("source_text", ""),
                requirement_ids=kf.get("requirement_ids", []),
                question_patterns=kf.get("question_patterns", []),
                answer=kf.get("answer"),
            )
        )
    return facts


def _draft_to_findings(draft: _LLMCompilerDraft) -> list[CompileFinding]:
    findings = []
    for f in draft.compile_findings:
        findings.append(
            CompileFinding(
                code=f.get("code", "UNKNOWN"),
                message=f.get("message", ""),
                severity=_to_severity(f.get("severity", "minor")),
                blocking=bool(f.get("blocking", False)),
                source_node_id=f.get("source_node_id", ""),
                suggestion=f.get("suggestion", ""),
            )
        )
    return findings


class LLMTaskCompiler:
    """主 LLM 编译器：Markdown -> TaskUnderstanding。"""

    def __init__(self, client: StructuredLLMClient | None = None):
        from outbound_eval.llm.structured_client import get_client

        self._client = client or get_client()

    async def compile(
        self,
        *,
        raw_markdown: str,
        model_config: ModelConfig,
    ) -> TaskUnderstanding:
        parser = MarkdownAstParser()
        ast = parser.parse(raw_markdown)

        messages = build_compiler_messages(raw_markdown, ast)
        result = await self._client.invoke_json(
            model_config=model_config,
            messages=messages,
            output_model=_LLMCompilerDraft,
            stage="compile_task",
            temperature=0.1,
        )
        draft: _LLMCompilerDraft = result.parsed

        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task_spec_dict = _draft_to_task_spec(draft, task_id)
        judge_plan = _draft_to_judge_plan(draft, task_id)
        risk_plan = _draft_to_risk_plan(draft, task_id)
        knowledge_facts = _draft_to_knowledge_facts(draft)
        findings = _draft_to_findings(draft)

        if result.repaired:
            findings.append(
                CompileFinding(
                    code="JSON_REPAIRED",
                    message="LLM 输出 JSON 格式异常，已自动修复",
                    severity="minor",
                    blocking=False,
                )
            )

        return TaskUnderstanding(
            task_spec=task_spec_dict,
            judge_plan=judge_plan,
            risk_plan=risk_plan,
            source_map={},
            compiler_notes=draft.compiler_notes,
            compile_findings=findings,
            knowledge_facts=knowledge_facts,
            raw_instruction=raw_markdown,
        )
