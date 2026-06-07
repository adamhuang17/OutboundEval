"""LLMTaskCompiler — 用 LLM 把任意 Markdown 任务说明编译成 TaskUnderstanding。"""
from __future__ import annotations

import inspect
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.compiler.markdown_ast import MarkdownAstParser
from outbound_eval.compiler.task_compiler_prompts import (
    STAGE_CONSTRAINT_COMPILE,
    STAGE_FLOW_COMPILE,
    STAGE_JUDGE_PLAN_BUILD,
    STAGE_KNOWLEDGE_COMPILE,
    STAGE_REQUIREMENT_SYNTH,
    STAGE_TASK_OUTLINE,
    build_stage_messages,
    select_stage_nodes,
)
from outbound_eval.compiler.compile_qa import CompileQAGate
from outbound_eval.compiler.variable_extractor import extract_variables
from outbound_eval.domain.schemas_markdown import MarkdownAst, MarkdownNode, SourceRef
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
from outbound_eval.llm.structured_client import model_runtime_profile
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


class _StageBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    compile_findings: list[dict[str, Any]] = Field(default_factory=list)
    compiler_notes: list[str] = Field(default_factory=list)


class _TaskOutlineDraft(_StageBase):
    task_name: str = ""
    role: str = ""
    objective: str = ""
    opening_line: str = ""
    section_intents: list[dict[str, Any]] = Field(default_factory=list)


class _FlowCompileDraft(_StageBase):
    flow_nodes: list[dict[str, Any]] = Field(default_factory=list)
    branch_rules: list[dict[str, Any]] = Field(default_factory=list)


class _KnowledgeCompileDraft(_StageBase):
    knowledge_facts: list[dict[str, Any]] = Field(default_factory=list)


class _ConstraintCompileDraft(_StageBase):
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_behaviors: list[dict[str, Any]] = Field(default_factory=list)
    termination_rules: list[dict[str, Any]] = Field(default_factory=list)
    risk_plan: dict[str, Any] = Field(default_factory=dict)


class _RequirementSynthDraft(_StageBase):
    requirements: list[dict[str, Any]] = Field(default_factory=list)


class _JudgePlanBuildDraft(_StageBase):
    judge_plan: dict[str, Any] = Field(default_factory=dict)


class CompileStageDiagnostic(BaseModel):
    model_config = ConfigDict(extra="allow")

    stage: str
    status: Literal["started", "completed", "fallback", "failed"]
    message: str = ""
    compile_id: str | None = None
    elapsed_ms: int | None = None
    duration_ms: int | None = None
    prompt_chars: int | None = None
    output_chars: int | None = None
    model_name: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    artifact: dict[str, Any] = Field(default_factory=dict)


StageCallback = Callable[[CompileStageDiagnostic], Awaitable[None] | None]


def _normalize_id(value: str, prefix: str, seq_map: dict[str, int]) -> str:
    """确保 id 有正确 prefix，不会重复。"""
    if value and value.startswith(prefix):
        return value
    seq_map[prefix] = seq_map.get(prefix, 0) + 1
    return f"{prefix}{seq_map[prefix]:03d}"


def _to_severity(value: str) -> str:
    mapping = {"critical": "critical", "major": "major", "minor": "minor"}
    return mapping.get(str(value).lower(), "major")


def _node_quote(node: MarkdownNode | None, fallback: str = "") -> str:
    if node is None:
        return fallback.strip()
    quote = node.raw_text or node.body or fallback
    return quote.strip()


def _node_source_ref(node: MarkdownNode) -> SourceRef:
    return SourceRef(
        source_node_id=node.id,
        heading_path=node.path,
        start_line=node.start_line,
        end_line=node.end_line,
        quote=_node_quote(node),
    )


def _build_source_map(ast: MarkdownAst) -> dict[str, SourceRef]:
    parser = MarkdownAstParser()
    nodes = parser.flatten(ast)
    if not nodes and ast.root:
        nodes = [ast.root]
    return {node.id: _node_source_ref(node) for node in nodes}


def _source_ref_for(source_map: dict[str, SourceRef], source_node_id: str, fallback: str = "") -> SourceRef:
    if source_node_id and source_node_id in source_map:
        return source_map[source_node_id]
    return SourceRef(
        source_node_id=source_node_id or "node_root",
        heading_path=[],
        start_line=0,
        end_line=0,
        quote=fallback.strip(),
    )


def _source_text_for(source_map: dict[str, SourceRef], source_node_id: str, fallback: str = "") -> str:
    return _source_ref_for(source_map, source_node_id, fallback).quote or fallback.strip()


def _draft_to_task_spec(
    draft: _LLMCompilerDraft,
    task_id: str,
    *,
    source_map: dict[str, SourceRef],
    raw_markdown: str,
) -> dict[str, Any]:
    """把 LLM draft 转成 TaskSpec-compatible dict。"""
    seq: dict[str, int] = {}

    requirements = []
    for r in draft.requirements:
        rid = _normalize_id(r.get("id", ""), "req.", seq)
        source_node_id = r.get("source_node_id", "")
        source_text = r.get("source_text", "") or _source_text_for(source_map, source_node_id, r.get("name", ""))
        requirements.append(
            {
                "id": rid,
                "name": r.get("name", "未命名需求"),
                "category": r.get("category", "task"),
                "source_section": source_node_id,
                "source_text": source_text,
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
        source_node_id = b.get("source_node_id", "")
        branch_rules.append(
            {
                "id": _normalize_id(b.get("id", ""), "branch.", seq),
                "name": b.get("name", ""),
                "condition": b.get("condition", ""),
                "source_text": b.get("source_text", "") or _source_text_for(source_map, source_node_id, b.get("condition", "")),
            }
        )

    knowledge_facts = []
    for kf in draft.knowledge_facts:
        source_node_id = kf.get("source_node_id", "")
        text = kf.get("text", "")
        source_text = kf.get("source_text", "") or _source_text_for(source_map, source_node_id, text)
        knowledge_facts.append(
            {
                "id": _normalize_id(kf.get("id", ""), "kf.", seq),
                "text": text or source_text[:200],
                "fact_type": kf.get("fact_type", "other"),
                "source_node_id": source_node_id,
                "source_text": source_text,
                "requirement_ids": kf.get("requirement_ids", []),
                "question_patterns": kf.get("question_patterns", []),
                "answer": kf.get("answer"),
            }
        )

    constraints = []
    for c in draft.constraints:
        source_node_id = c.get("source_node_id", "")
        constraints.append(
            {
                "id": _normalize_id(c.get("id", ""), "con.", seq),
                "name": c.get("name", ""),
                "rule_text": c.get("rule_text", "") or _source_text_for(source_map, source_node_id, c.get("name", "")),
                "severity": _to_severity(c.get("severity", "major")),
            }
        )

    forbidden_behaviors = []
    for fb in draft.forbidden_behaviors:
        source_node_id = fb.get("source_node_id", "")
        forbidden_behaviors.append(
            {
                "id": _normalize_id(fb.get("id", ""), "fb.", seq),
                "name": fb.get("name", ""),
                "description": fb.get("description", ""),
                "severity": _to_severity(fb.get("severity", "critical")),
                "cap_score": float(fb.get("cap_score", 60.0)),
                "source_text": fb.get("source_text", "") or _source_text_for(source_map, source_node_id, fb.get("description", "")),
            }
        )

    termination_rules = []
    for t in draft.termination_rules:
        source_node_id = t.get("source_node_id", "")
        termination_rules.append(
            {
                "id": _normalize_id(t.get("id", ""), "term.", seq),
                "name": t.get("name", ""),
                "condition": t.get("condition", ""),
                "source_text": t.get("source_text", "") or _source_text_for(source_map, source_node_id, t.get("condition", "")),
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
    for kf in knowledge_facts:
        if kf.get("fact_type", "faq") == "faq" and kf.get("answer"):
            faq_facts.append(
                {
                    "id": _normalize_id(str(kf.get("id", "")).replace("kf.", "faq."), "faq.", seq),
                    "question": kf.get("question_patterns", [""])[0] if kf.get("question_patterns") else kf.get("text", ""),
                    "answer": kf.get("answer", ""),
                    "grounding_source": kf.get("source_text", "原文"),
                    "requirement_ids": kf.get("requirement_ids", []),
                }
            )

    source_map_json = {key: value.model_dump(mode="json") for key, value in source_map.items()}
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
        "knowledge_facts": knowledge_facts,
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
        "source_map": source_map_json,
        "source_text": raw_markdown,
    }


def _draft_to_judge_plan(draft: _LLMCompilerDraft, task_id: str, source_map: dict[str, SourceRef]) -> JudgePlan:
    jp_raw = draft.judge_plan
    seq: dict[str, int] = {}
    judge_points = []
    for jp in jp_raw.get("judge_points", []):
        source_node_id = jp.get("source_node_id", "")
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
                source_node_id=source_node_id,
                source_text=jp.get("source_text", "") or _source_text_for(source_map, source_node_id, jp.get("criterion", "")),
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


def _draft_to_knowledge_facts(draft: _LLMCompilerDraft, source_map: dict[str, SourceRef]) -> list[KnowledgeFact]:
    facts = []
    seq: dict[str, int] = {}
    for kf in draft.knowledge_facts:
        source_node_id = kf.get("source_node_id", "")
        text = kf.get("text", "")
        source_text = kf.get("source_text", "") or _source_text_for(source_map, source_node_id, text)
        facts.append(
            KnowledgeFact(
                id=_normalize_id(kf.get("id", ""), "kf.", seq),
                text=text or source_text[:200],
                fact_type=kf.get("fact_type", "faq"),
                source_node_id=source_node_id,
                source_text=source_text,
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


def _prompt_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(message.get("content", "")) for message in messages)


def _flatten_nodes(ast: MarkdownAst) -> list[MarkdownNode]:
    parser = MarkdownAstParser()
    nodes = parser.flatten(ast)
    if not nodes and ast.root:
        nodes = [ast.root]
    return nodes


def _node_excerpt(node: MarkdownNode | None, max_chars: int = 240) -> str:
    text = _node_quote(node)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _node_has_keywords(node: MarkdownNode, keywords: tuple[str, ...]) -> bool:
    haystack = " ".join([node.heading or "", " ".join(node.path or []), node.body or ""]).lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def _find_first_node(nodes: list[MarkdownNode], keywords: tuple[str, ...]) -> MarkdownNode | None:
    return next((node for node in nodes if _node_has_keywords(node, keywords)), None)


def _line_items(text: str) -> list[str]:
    items: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if line:
            items.append(line)
    return items


def _stage_summary(parsed: BaseModel) -> dict[str, Any]:
    data = parsed.model_dump(mode="json")
    summary: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            summary[f"{key}_keys"] = list(value.keys())[:8]
        elif key in {"task_name", "role", "objective", "opening_line"}:
            summary[key] = value
    return summary


def _append_stage_finding(parsed: BaseModel, *, code: str, message: str, stage: str, severity: str = "minor") -> None:
    findings = getattr(parsed, "compile_findings", None)
    if isinstance(findings, list):
        findings.append(
            {
                "code": code,
                "message": message,
                "severity": severity,
                "blocking": False,
                "source_node_id": "",
                "suggestion": f"Review compile stage {stage}.",
            }
        )


def _local_task_outline(ast: MarkdownAst) -> _TaskOutlineDraft:
    nodes = _flatten_nodes(ast)
    first = nodes[0] if nodes else ast.root
    role_node = _find_first_node(nodes, ("role", "persona", "caller", "agent", "角色", "身份", "客服"))
    objective_node = _find_first_node(nodes, ("objective", "task", "goal", "purpose", "目标", "任务", "目的"))
    opening_node = _find_first_node(nodes, ("opening", "greeting", "开场", "开场白", "问候"))
    task_name = (first.heading if first else "").strip() or "Outbound Task"
    role = _node_excerpt(role_node, 300)
    objective = _node_excerpt(objective_node, 360)
    if not objective and first:
        objective = _node_excerpt(first, 360)
    opening_line = ""
    if opening_node:
        lines = _line_items(opening_node.body or opening_node.raw_text)
        opening_line = lines[0] if lines else _node_excerpt(opening_node, 160)
    intents = []
    for node in nodes:
        intent = "other"
        if _node_has_keywords(node, ("role", "角色", "身份")):
            intent = "role"
        elif _node_has_keywords(node, ("opening", "开场", "问候")):
            intent = "opening"
        elif _node_has_keywords(node, ("flow", "step", "流程", "步骤", "话术")):
            intent = "flow"
        elif _node_has_keywords(node, ("faq", "knowledge", "知识", "问答", "口径")):
            intent = "knowledge"
        elif _node_has_keywords(node, ("constraint", "forbidden", "禁止", "不得", "约束")):
            intent = "constraint"
        elif _node_has_keywords(node, ("termination", "stop", "结束", "终止")):
            intent = "termination"
        intents.append({"source_node_id": node.id, "intent": intent, "reason": node.heading})
    return _TaskOutlineDraft(
        task_name=task_name[:40],
        role=role or "Outbound caller",
        objective=objective,
        opening_line=opening_line,
        section_intents=intents,
        compile_findings=[
            {
                "code": "LOCAL_TASK_OUTLINE",
                "message": "task_outline used deterministic AST fallback.",
                "severity": "minor",
                "blocking": False,
                "source_node_id": first.id if first else "",
                "suggestion": "Use a stronger compiler model for richer outline semantics.",
            }
        ],
    )


def _local_flow_compile(ast: MarkdownAst) -> _FlowCompileDraft:
    nodes = select_stage_nodes(ast, STAGE_FLOW_COMPILE)
    flow_nodes: list[dict[str, Any]] = []
    branch_rules: list[dict[str, Any]] = []
    branch_markers = ("if ", "when ", "otherwise", "else", "若", "如果", "当", "否则", "分支")
    for node in nodes:
        raw_items = [bullet.text for bullet in node.bullets] or _line_items(node.body or node.raw_text)
        for item in raw_items:
            if any(marker in item.lower() for marker in branch_markers):
                branch_rules.append(
                    {
                        "id": f"branch.{len(branch_rules)+1:03d}",
                        "name": item[:40],
                        "condition": item,
                        "source_node_id": node.id,
                        "source_text": item,
                    }
                )
                continue
            flow_nodes.append(
                {
                    "id": f"flow.{len(flow_nodes)+1:03d}",
                    "name": item[:40] or node.heading or f"Step {len(flow_nodes)+1}",
                    "instruction": item or _node_excerpt(node),
                    "requirement_ids": [],
                    "order": len(flow_nodes),
                    "source_node_id": node.id,
                    "source_text": item or _node_excerpt(node),
                }
            )
    if not flow_nodes and nodes:
        node = nodes[0]
        flow_nodes.append(
            {
                "id": "flow.001",
                "name": node.heading or "Main step",
                "instruction": _node_excerpt(node),
                "requirement_ids": [],
                "order": 0,
                "source_node_id": node.id,
                "source_text": _node_excerpt(node),
            }
        )
    return _FlowCompileDraft(
        flow_nodes=flow_nodes[:16],
        branch_rules=branch_rules[:12],
        compile_findings=[
            {
                "code": "LOCAL_FLOW_COMPILE",
                "message": "flow_compile used deterministic AST fallback.",
                "severity": "minor",
                "blocking": False,
                "source_node_id": nodes[0].id if nodes else "",
                "suggestion": "Review flow extraction if headings are ambiguous.",
            }
        ],
    )


def _local_knowledge_compile(ast: MarkdownAst) -> _KnowledgeCompileDraft:
    nodes = select_stage_nodes(ast, STAGE_KNOWLEDGE_COMPILE)
    facts: list[dict[str, Any]] = []
    for node in nodes:
        lines = _line_items(node.body or node.raw_text)
        pending_q: str | None = None
        for item in lines:
            q_match = re.match(r"^(?:q|Q|问|问题)[:：]\s*(.+)$", item)
            a_match = re.match(r"^(?:a|A|答|答案)[:：]\s*(.+)$", item)
            if q_match:
                pending_q = q_match.group(1).strip()
                continue
            if a_match and pending_q:
                answer = a_match.group(1).strip()
                facts.append(
                    {
                        "id": f"kf.{len(facts)+1:03d}",
                        "text": pending_q,
                        "fact_type": "faq",
                        "source_node_id": node.id,
                        "source_text": f"{pending_q}\n{answer}",
                        "requirement_ids": [],
                        "question_patterns": [pending_q],
                        "answer": answer,
                    }
                )
                pending_q = None
                continue
            if len(item) >= 8:
                facts.append(
                    {
                        "id": f"kf.{len(facts)+1:03d}",
                        "text": item[:220],
                        "fact_type": "business_rule",
                        "source_node_id": node.id,
                        "source_text": item,
                        "requirement_ids": [],
                        "question_patterns": [],
                        "answer": None,
                    }
                )
    return _KnowledgeCompileDraft(
        knowledge_facts=facts[:24],
        compile_findings=[
            {
                "code": "LOCAL_KNOWLEDGE_COMPILE",
                "message": "knowledge_compile used deterministic AST fallback.",
                "severity": "minor",
                "blocking": False,
                "source_node_id": nodes[0].id if nodes else "",
                "suggestion": "Review knowledge facts for FAQ answer completeness.",
            }
        ],
    )


def _local_constraint_compile(ast: MarkdownAst) -> _ConstraintCompileDraft:
    nodes = select_stage_nodes(ast, STAGE_CONSTRAINT_COMPILE)
    constraints: list[dict[str, Any]] = []
    forbidden: list[dict[str, Any]] = []
    termination: list[dict[str, Any]] = []
    forbidden_markers = ("禁止", "不得", "不能", "不允许", "严禁", "must not", "do not", "never")
    termination_markers = ("结束", "终止", "停止", "挂断", "完成", "stop", "terminate", "end")
    for node in nodes:
        for item in ([bullet.text for bullet in node.bullets] or _line_items(node.body or node.raw_text)):
            lowered = item.lower()
            if any(marker in lowered for marker in termination_markers):
                termination.append(
                    {
                        "id": f"term.{len(termination)+1:03d}",
                        "name": item[:40],
                        "condition": item,
                        "source_node_id": node.id,
                        "source_text": item,
                    }
                )
            elif any(marker in lowered for marker in forbidden_markers):
                forbidden.append(
                    {
                        "id": f"fb.{len(forbidden)+1:03d}",
                        "name": item[:40],
                        "description": item,
                        "severity": "critical",
                        "cap_score": 60.0,
                        "source_node_id": node.id,
                        "source_text": item,
                    }
                )
            else:
                constraints.append(
                    {
                        "id": f"con.{len(constraints)+1:03d}",
                        "name": item[:40],
                        "rule_text": item,
                        "severity": "major",
                        "source_node_id": node.id,
                        "source_text": item,
                    }
                )
    return _ConstraintCompileDraft(
        constraints=constraints[:20],
        forbidden_behaviors=forbidden[:20],
        termination_rules=termination[:12],
        risk_plan={"detected_risks": [], "coverage_requirements": []},
        compile_findings=[
            {
                "code": "LOCAL_CONSTRAINT_COMPILE",
                "message": "constraint_compile used deterministic AST fallback.",
                "severity": "minor",
                "blocking": False,
                "source_node_id": nodes[0].id if nodes else "",
                "suggestion": "Review forbidden behavior and termination grouping.",
            }
        ],
    )


def _local_requirement_synth(
    outline: _TaskOutlineDraft,
    flow: _FlowCompileDraft,
    knowledge: _KnowledgeCompileDraft,
    constraints: _ConstraintCompileDraft,
    source_map: dict[str, SourceRef],
) -> _RequirementSynthDraft:
    requirements: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    fallback_source = next(iter(source_map.keys()), "node_root")

    def add_req(
        name: str,
        category: str,
        source_node_id: str,
        source_text: str,
        check_method: str,
        severity: str = "major",
        tags: list[str] | None = None,
    ) -> str:
        clean_name = (name or source_text or category).strip()
        clean_source = source_node_id or fallback_source
        clean_text = (source_text or _source_text_for(source_map, clean_source, clean_name) or clean_name).strip()
        key = (category, clean_name[:80], clean_source)
        if key in seen:
            return next(req["id"] for req in requirements if (req["category"], req["name"][:80], req["source_node_id"]) == key)
        seen.add(key)
        rid = f"req.{len(requirements)+1:03d}"
        requirements.append(
            {
                "id": rid,
                "name": clean_name[:120],
                "category": category,
                "source_node_id": clean_source,
                "source_text": clean_text,
                "check_method": check_method,
                "severity": severity,
                "tags": tags or [],
            }
        )
        return rid

    add_req(
        outline.objective or outline.task_name or "Complete task objective",
        "task",
        (outline.section_intents[0].get("source_node_id") if outline.section_intents else fallback_source),
        outline.objective or outline.task_name,
        "llm",
        "critical",
        ["objective"],
    )
    for item in flow.flow_nodes:
        rid = add_req(item.get("name") or item.get("instruction", ""), "flow", item.get("source_node_id", ""), item.get("source_text") or item.get("instruction", ""), "flow")
        item["requirement_ids"] = item.get("requirement_ids") or [rid]
    for item in flow.branch_rules:
        rid = add_req(item.get("name") or item.get("condition", ""), "flow", item.get("source_node_id", ""), item.get("source_text") or item.get("condition", ""), "flow", tags=["branch"])
        item["requirement_id"] = item.get("requirement_id") or rid
    for item in knowledge.knowledge_facts:
        rid = add_req(item.get("text", ""), "knowledge", item.get("source_node_id", ""), item.get("source_text") or item.get("text", ""), "knowledge")
        item["requirement_ids"] = item.get("requirement_ids") or [rid]
    for item in constraints.constraints:
        rid = add_req(item.get("name") or item.get("rule_text", ""), "constraint", item.get("source_node_id", ""), item.get("source_text") or item.get("rule_text", ""), "rule", item.get("severity", "major"))
        item["requirement_id"] = item.get("requirement_id") or rid
    for item in constraints.forbidden_behaviors:
        rid = add_req(item.get("name") or item.get("description", ""), "constraint", item.get("source_node_id", ""), item.get("source_text") or item.get("description", ""), "rule", item.get("severity", "critical"), tags=["forbidden"])
        item["requirement_id"] = item.get("requirement_id") or rid
    for item in constraints.termination_rules:
        rid = add_req(item.get("name") or item.get("condition", ""), "termination", item.get("source_node_id", ""), item.get("source_text") or item.get("condition", ""), "rule")
        item["requirement_id"] = item.get("requirement_id") or rid

    return _RequirementSynthDraft(
        requirements=requirements,
        compile_findings=[
            {
                "code": "LOCAL_REQUIREMENT_SYNTH",
                "message": "requirement_synth used deterministic artifact fallback.",
                "severity": "minor",
                "blocking": False,
                "source_node_id": fallback_source,
                "suggestion": "Review requirement granularity before scenario generation.",
            }
        ],
    )


def _dimension_for_requirement(req: dict[str, Any]) -> str:
    category = str(req.get("category", "task"))
    tags = set(req.get("tags") or [])
    if category == "flow":
        return "flow_following"
    if category == "knowledge":
        return "knowledge_correctness"
    if category == "constraint":
        return "safety_compliance" if "forbidden" in tags else "constraint_following"
    if category == "termination":
        return "exception_handling"
    if category == "exception":
        return "exception_handling"
    return "task_completion"


def _local_judge_plan_build(requirements: _RequirementSynthDraft) -> _JudgePlanBuildDraft:
    judge_points: list[dict[str, Any]] = []
    for req in requirements.requirements[:18]:
        dimension = _dimension_for_requirement(req)
        criterion = req.get("name") or req.get("source_text", "")
        judge_points.append(
            {
                "id": f"jp.{len(judge_points)+1:03d}",
                "dimension": dimension,
                "criterion": criterion,
                "pass_criteria": f"Assistant satisfies requirement: {criterion}",
                "partial_criteria": f"Assistant partially satisfies requirement: {criterion}",
                "fail_criteria": f"Assistant misses or violates requirement: {criterion}",
                "severity": req.get("severity", "major"),
                "weight": 1.0,
                "source_node_id": req.get("source_node_id", ""),
                "source_text": req.get("source_text", ""),
                "linked_requirement_ids": [req.get("id", "")],
                "evaluator": "hybrid" if req.get("check_method") in {"rule", "flow", "knowledge"} else "llm",
            }
        )
    dims = {jp["dimension"] for jp in judge_points}
    weight = round(1.0 / len(dims), 4) if dims else 1.0
    dimension_weights = {dim: weight for dim in sorted(dims)}
    return _JudgePlanBuildDraft(
        judge_plan={"judge_points": judge_points, "dimension_weights": dimension_weights},
        compile_findings=[
            {
                "code": "LOCAL_JUDGE_PLAN_BUILD",
                "message": "judge_plan_build used deterministic requirement fallback.",
                "severity": "minor",
                "blocking": False,
                "source_node_id": "",
                "suggestion": "Review pass/fail criteria for scoring nuance.",
            }
        ],
    )


def _sanitize_requirements(requirements: list[dict[str, Any]], source_map: dict[str, SourceRef]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    sanitized: list[dict[str, Any]] = []
    id_map: dict[str, str] = {}
    seen: set[str] = set()
    fallback_source = next(iter(source_map.keys()), "node_root")
    for item in requirements:
        old_id = str(item.get("id", ""))
        new_id = old_id if old_id.startswith("req.") and old_id not in seen else ""
        if not new_id:
            new_id = f"req.{len(sanitized)+1:03d}"
            while new_id in seen:
                new_id = f"req.{len(sanitized)+2:03d}"
        seen.add(new_id)
        copy = dict(item)
        copy["id"] = new_id
        copy["source_node_id"] = copy.get("source_node_id") or copy.get("source_section") or fallback_source
        copy["source_text"] = copy.get("source_text") or _source_text_for(source_map, copy["source_node_id"], copy.get("name", ""))
        copy["category"] = copy.get("category") or "task"
        copy["check_method"] = copy.get("check_method") or "llm"
        copy["severity"] = _to_severity(copy.get("severity", "major"))
        sanitized.append(copy)
        if old_id:
            id_map[old_id] = new_id
    return sanitized, id_map


def _remap_requirement_ids(values: list[str], id_map: dict[str, str], valid_ids: set[str]) -> list[str]:
    remapped = [id_map.get(value, value) for value in values if id_map.get(value, value) in valid_ids]
    return list(dict.fromkeys(remapped))


def _link_components_to_requirements(draft: _LLMCompilerDraft, source_map: dict[str, SourceRef]) -> None:
    draft.requirements, id_map = _sanitize_requirements(draft.requirements, source_map)
    valid_ids = {req["id"] for req in draft.requirements}
    by_category: dict[str, list[dict[str, Any]]] = {}
    by_source: dict[str, list[dict[str, Any]]] = {}
    for req in draft.requirements:
        by_category.setdefault(req.get("category", "task"), []).append(req)
        by_source.setdefault(req.get("source_node_id", ""), []).append(req)

    def pick(category: str, source_node_id: str) -> list[str]:
        candidates = [req for req in by_source.get(source_node_id, []) if req.get("category") == category]
        if not candidates:
            candidates = by_category.get(category, [])
        if not candidates:
            candidates = draft.requirements[:1]
        return [candidates[0]["id"]] if candidates else []

    for item in draft.flow_nodes:
        item["requirement_ids"] = _remap_requirement_ids(item.get("requirement_ids", []), id_map, valid_ids) or pick("flow", item.get("source_node_id", ""))
    for item in draft.knowledge_facts:
        item["requirement_ids"] = _remap_requirement_ids(item.get("requirement_ids", []), id_map, valid_ids) or pick("knowledge", item.get("source_node_id", ""))
    def pick_one(category: str, item: dict[str, Any]) -> str | None:
        current = item.get("requirement_id", "")
        remapped = _remap_requirement_ids([current], id_map, valid_ids) if current else []
        if remapped:
            return remapped[0]
        picked = pick(category, item.get("source_node_id", ""))
        return picked[0] if picked else None

    for item in draft.constraints:
        item["requirement_id"] = pick_one("constraint", item)
    for item in draft.forbidden_behaviors:
        item["requirement_id"] = pick_one("constraint", item)
    for item in draft.termination_rules:
        item["requirement_id"] = pick_one("termination", item)

    judge_plan = draft.judge_plan or {}
    for jp in judge_plan.get("judge_points", []):
        linked = _remap_requirement_ids(jp.get("linked_requirement_ids", []), id_map, valid_ids)
        if not linked:
            dimension = jp.get("dimension", "task_completion")
            category = {
                "flow_following": "flow",
                "knowledge_correctness": "knowledge",
                "constraint_following": "constraint",
                "safety_compliance": "constraint",
                "exception_handling": "termination",
            }.get(dimension, "task")
            linked = pick(category, jp.get("source_node_id", ""))
        jp["linked_requirement_ids"] = linked


def _merge_stage_findings(*stages: _StageBase) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for stage in stages:
        findings.extend(stage.compile_findings)
    return findings


def _merge_stage_notes(*stages: _StageBase) -> list[str]:
    notes: list[str] = []
    for stage in stages:
        notes.extend(stage.compiler_notes)
    return notes


def _build_draft_from_stages(
    *,
    outline: _TaskOutlineDraft,
    flow: _FlowCompileDraft,
    knowledge: _KnowledgeCompileDraft,
    constraints: _ConstraintCompileDraft,
    requirements: _RequirementSynthDraft,
    judge_plan: _JudgePlanBuildDraft,
    raw_markdown: str,
) -> _LLMCompilerDraft:
    variables = [item.model_dump(mode="json") for item in extract_variables(raw_markdown)]
    draft = _LLMCompilerDraft(
        task_name=outline.task_name or "Outbound Task",
        role=outline.role or "Outbound caller",
        objective=outline.objective or outline.task_name or "Complete outbound task.",
        opening_line=outline.opening_line,
        requirements=requirements.requirements,
        flow_nodes=flow.flow_nodes,
        branch_rules=flow.branch_rules,
        knowledge_facts=knowledge.knowledge_facts,
        constraints=constraints.constraints,
        forbidden_behaviors=constraints.forbidden_behaviors,
        termination_rules=constraints.termination_rules,
        variables=variables,
        judge_plan=judge_plan.judge_plan,
        risk_plan=constraints.risk_plan,
        compile_findings=_merge_stage_findings(outline, flow, knowledge, constraints, requirements, judge_plan),
        compiler_notes=_merge_stage_notes(outline, flow, knowledge, constraints, requirements, judge_plan),
    )
    return draft


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
        source_map = _build_source_map(ast)
        task_spec_dict = _draft_to_task_spec(draft, task_id, source_map=source_map, raw_markdown=raw_markdown)
        judge_plan = _draft_to_judge_plan(draft, task_id, source_map)
        risk_plan = _draft_to_risk_plan(draft, task_id)
        knowledge_facts = _draft_to_knowledge_facts(draft, source_map)
        findings = _draft_to_findings(draft)

        artifact_source_map = dict(source_map)
        for req in task_spec_dict.get("requirements", []):
            artifact_source_map[req["id"]] = _source_ref_for(source_map, req.get("source_section", ""), req.get("source_text", ""))
        for kf in knowledge_facts:
            artifact_source_map[kf.id] = _source_ref_for(source_map, kf.source_node_id, kf.source_text)
        for jp in judge_plan.judge_points:
            artifact_source_map[jp.id] = _source_ref_for(source_map, jp.source_node_id, jp.source_text)
        task_spec_dict["source_map"] = {key: value.model_dump(mode="json") for key, value in artifact_source_map.items()}

        if result.repaired:
            findings.append(
                CompileFinding(
                    code="JSON_REPAIRED",
                    message="LLM 输出 JSON 格式异常，已自动修复",
                    severity="minor",
                    blocking=False,
                )
            )

        understanding = TaskUnderstanding(
            task_spec=task_spec_dict,
            judge_plan=judge_plan,
            risk_plan=risk_plan,
            source_map=artifact_source_map,
            compiler_notes=draft.compiler_notes,
            compile_findings=findings,
            knowledge_facts=knowledge_facts,
            raw_instruction=raw_markdown,
        )
        qa = CompileQAGate().validate(understanding)
        if qa.findings:
            understanding.compile_findings.extend(
                CompileFinding(
                    code=str(finding.metadata.get("code", "COMPILE_QA")),
                    message=finding.detail,
                    severity=finding.severity,
                    blocking=finding.blocking,
                    source_node_id=finding.requirement_ref or "",
                    suggestion=finding.suggested_fix,
                )
                for finding in qa.findings
            )
        return understanding


class LLMTaskCompiler:
    """Staged compiler: Markdown -> staged artifacts -> TaskUnderstanding."""

    def __init__(self, client: StructuredLLMClient | None = None):
        from outbound_eval.llm.structured_client import get_client

        self._client = client or get_client()
        self.last_diagnostics: list[CompileStageDiagnostic] = []
        self.last_stage_results: dict[str, Any] = {}

    async def compile(
        self,
        *,
        raw_markdown: str,
        model_config: ModelConfig,
        stage_callback: StageCallback | None = None,
        compile_id: str | None = None,
        fallback_configs: list[ModelConfig] | None = None,
    ) -> TaskUnderstanding:
        started = time.perf_counter()
        self.last_diagnostics = []
        self.last_stage_results = {}

        parser = MarkdownAstParser()
        ast = parser.parse(raw_markdown)
        source_map = _build_source_map(ast)
        task_id = f"task_{uuid.uuid4().hex[:8]}"

        await self._emit(
            stage_callback,
            CompileStageDiagnostic(
                stage="markdown_ast",
                status="completed",
                message="Markdown AST parsed.",
                compile_id=compile_id,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                artifact={
                    "nodes_count": len(_flatten_nodes(ast)),
                    "parse_warnings": ast.parse_warnings,
                    "source_nodes": list(source_map.keys())[:20],
                },
            ),
        )

        outline = await self._run_stage(
            stage=STAGE_TASK_OUTLINE,
            ast=ast,
            model_config=model_config,
            output_model=_TaskOutlineDraft,
            fallback=lambda: _local_task_outline(ast),
            stage_callback=stage_callback,
            compile_id=compile_id,
            fallback_configs=fallback_configs,
        )
        prior_outline = {"task_outline": outline.model_dump(mode="json")}

        flow = await self._run_stage(
            stage=STAGE_FLOW_COMPILE,
            ast=ast,
            model_config=model_config,
            output_model=_FlowCompileDraft,
            fallback=lambda: _local_flow_compile(ast),
            stage_callback=stage_callback,
            compile_id=compile_id,
            prior_artifacts=prior_outline,
            fallback_configs=fallback_configs,
        )
        knowledge = await self._run_stage(
            stage=STAGE_KNOWLEDGE_COMPILE,
            ast=ast,
            model_config=model_config,
            output_model=_KnowledgeCompileDraft,
            fallback=lambda: _local_knowledge_compile(ast),
            stage_callback=stage_callback,
            compile_id=compile_id,
            prior_artifacts=prior_outline,
            fallback_configs=fallback_configs,
        )
        constraints = await self._run_stage(
            stage=STAGE_CONSTRAINT_COMPILE,
            ast=ast,
            model_config=model_config,
            output_model=_ConstraintCompileDraft,
            fallback=lambda: _local_constraint_compile(ast),
            stage_callback=stage_callback,
            compile_id=compile_id,
            prior_artifacts=prior_outline,
            fallback_configs=fallback_configs,
        )

        prior_requirements = {
            **prior_outline,
            "flow_compile": flow.model_dump(mode="json"),
            "knowledge_compile": knowledge.model_dump(mode="json"),
            "constraint_compile": constraints.model_dump(mode="json"),
        }
        requirements = await self._run_stage(
            stage=STAGE_REQUIREMENT_SYNTH,
            ast=ast,
            model_config=model_config,
            output_model=_RequirementSynthDraft,
            fallback=lambda: _local_requirement_synth(outline, flow, knowledge, constraints, source_map),
            stage_callback=stage_callback,
            compile_id=compile_id,
            prior_artifacts=prior_requirements,
            fallback_configs=fallback_configs,
        )
        if not requirements.requirements:
            requirements = _local_requirement_synth(outline, flow, knowledge, constraints, source_map)
            await self._emit(
                stage_callback,
                CompileStageDiagnostic(
                    stage=STAGE_REQUIREMENT_SYNTH,
                    status="fallback",
                    message="Requirement synthesis returned empty output; local fallback filled requirements.",
                    compile_id=compile_id,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    artifact=_stage_summary(requirements),
                ),
            )

        prior_judge = {
            **prior_requirements,
            "requirement_synth": requirements.model_dump(mode="json"),
        }
        judge_plan_stage = await self._run_stage(
            stage=STAGE_JUDGE_PLAN_BUILD,
            ast=ast,
            model_config=model_config,
            output_model=_JudgePlanBuildDraft,
            fallback=lambda: _local_judge_plan_build(requirements),
            stage_callback=stage_callback,
            compile_id=compile_id,
            prior_artifacts=prior_judge,
            fallback_configs=fallback_configs,
        )
        if not (judge_plan_stage.judge_plan or {}).get("judge_points"):
            judge_plan_stage = _local_judge_plan_build(requirements)
            await self._emit(
                stage_callback,
                CompileStageDiagnostic(
                    stage=STAGE_JUDGE_PLAN_BUILD,
                    status="fallback",
                    message="Judge plan returned empty output; local fallback filled judge points.",
                    compile_id=compile_id,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    artifact=_stage_summary(judge_plan_stage),
                ),
            )

        await self._emit(
            stage_callback,
            CompileStageDiagnostic(
                stage="local_assemble",
                status="started",
                message="Assembling TaskUnderstanding locally.",
                compile_id=compile_id,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ),
        )
        draft = _build_draft_from_stages(
            outline=outline,
            flow=flow,
            knowledge=knowledge,
            constraints=constraints,
            requirements=requirements,
            judge_plan=judge_plan_stage,
            raw_markdown=raw_markdown,
        )
        _link_components_to_requirements(draft, source_map)

        task_spec_dict = _draft_to_task_spec(draft, task_id, source_map=source_map, raw_markdown=raw_markdown)
        judge_plan = _draft_to_judge_plan(draft, task_id, source_map)
        risk_plan = _draft_to_risk_plan(draft, task_id)
        knowledge_facts = _draft_to_knowledge_facts(draft, source_map)
        findings = _draft_to_findings(draft)

        artifact_source_map = dict(source_map)
        for req in task_spec_dict.get("requirements", []):
            artifact_source_map[req["id"]] = _source_ref_for(source_map, req.get("source_section", ""), req.get("source_text", ""))
        for kf in knowledge_facts:
            artifact_source_map[kf.id] = _source_ref_for(source_map, kf.source_node_id, kf.source_text)
        for jp in judge_plan.judge_points:
            artifact_source_map[jp.id] = _source_ref_for(source_map, jp.source_node_id, jp.source_text)
        task_spec_dict["source_map"] = {key: value.model_dump(mode="json") for key, value in artifact_source_map.items()}
        task_spec_dict.setdefault("metadata", {})
        task_spec_dict["metadata"]["compile_pipeline"] = {
            "mode": "staged",
            "compile_id": compile_id,
            "stage_results": self.last_stage_results,
            "diagnostics": [item.model_dump(mode="json") for item in self.last_diagnostics],
        }

        understanding = TaskUnderstanding(
            task_spec=task_spec_dict,
            judge_plan=judge_plan,
            risk_plan=risk_plan,
            source_map=artifact_source_map,
            compiler_notes=draft.compiler_notes,
            compile_findings=findings,
            knowledge_facts=knowledge_facts,
            raw_instruction=raw_markdown,
        )
        qa = CompileQAGate().validate(understanding)
        if qa.findings:
            understanding.compile_findings.extend(
                CompileFinding(
                    code=str(finding.metadata.get("code", "COMPILE_QA")),
                    message=finding.detail,
                    severity=finding.severity,
                    blocking=finding.blocking,
                    source_node_id=finding.requirement_ref or "",
                    suggestion=finding.suggested_fix,
                )
                for finding in qa.findings
            )

        self.last_stage_results["local_assemble"] = {
            "status": "completed",
            "task_id": task_id,
            "requirements_count": len(task_spec_dict.get("requirements", [])),
            "judge_points_count": len(judge_plan.judge_points),
            "findings_count": len(understanding.compile_findings),
        }
        await self._emit(
            stage_callback,
            CompileStageDiagnostic(
                stage="local_assemble",
                status="completed",
                message="TaskUnderstanding assembled.",
                compile_id=compile_id,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                artifact=self.last_stage_results["local_assemble"],
            ),
        )
        return understanding

    async def _run_stage(
        self,
        *,
        stage: str,
        ast: MarkdownAst,
        model_config: ModelConfig,
        output_model: type[_StageBase],
        fallback: Callable[[], _StageBase],
        stage_callback: StageCallback | None,
        compile_id: str | None,
        prior_artifacts: dict[str, Any] | None = None,
        fallback_configs: list[ModelConfig] | None = None,
    ) -> _StageBase:
        profile = model_runtime_profile(model_config)
        messages = build_stage_messages(stage=stage, ast=ast, prior_artifacts=prior_artifacts)
        prompt_size = _prompt_chars(messages)
        stage_started = time.perf_counter()
        await self._emit(
            stage_callback,
            CompileStageDiagnostic(
                stage=stage,
                status="started",
                message=f"Running {stage}.",
                compile_id=compile_id,
                prompt_chars=prompt_size,
                model_name=model_config.model_name,
            ),
        )
        try:
            result = await self._client.invoke_json(
                model_config=model_config,
                messages=messages,
                output_model=output_model,
                stage=stage,
                temperature=0.1,
                max_retries=profile.max_retries,
                stage_timeout=profile.max_stage_timeout,
                response_format=profile.response_format,
                fallback_configs=fallback_configs,
            )
            parsed = result.parsed
            if result.repaired:
                _append_stage_finding(
                    parsed,
                    code="JSON_REPAIRED",
                    message=f"{stage} JSON output was repaired before validation.",
                    stage=stage,
                )
            artifact = parsed.model_dump(mode="json")
            self.last_stage_results[stage] = {
                "status": "completed",
                "artifact": artifact,
                "warnings": result.warnings,
                "duration_ms": result.duration_ms,
                "prompt_chars": prompt_size,
                "output_chars": len(result.raw_text),
                "model_name": result.model_name,
            }
            await self._emit(
                stage_callback,
                CompileStageDiagnostic(
                    stage=stage,
                    status="completed",
                    message=f"{stage} completed.",
                    compile_id=compile_id,
                    duration_ms=result.duration_ms,
                    prompt_chars=prompt_size,
                    output_chars=len(result.raw_text),
                    model_name=result.model_name,
                    warnings=result.warnings,
                    artifact=_stage_summary(parsed),
                ),
            )
            return parsed
        except Exception as exc:
            parsed = fallback()
            _append_stage_finding(
                parsed,
                code="STAGE_LLM_FALLBACK",
                message=f"{stage} LLM stage failed and local fallback was used: {exc}",
                stage=stage,
                severity="major",
            )
            artifact = parsed.model_dump(mode="json")
            duration_ms = int((time.perf_counter() - stage_started) * 1000)
            self.last_stage_results[stage] = {
                "status": "fallback",
                "artifact": artifact,
                "error": str(exc),
                "duration_ms": duration_ms,
                "prompt_chars": prompt_size,
                "model_name": model_config.model_name,
            }
            await self._emit(
                stage_callback,
                CompileStageDiagnostic(
                    stage=stage,
                    status="fallback",
                    message=f"{stage} model call failed; local fallback used.",
                    compile_id=compile_id,
                    duration_ms=duration_ms,
                    prompt_chars=prompt_size,
                    model_name=model_config.model_name,
                    error=str(exc),
                    artifact=_stage_summary(parsed),
                ),
            )
            return parsed

    async def _emit(self, callback: StageCallback | None, event: CompileStageDiagnostic) -> None:
        self.last_diagnostics.append(event)
        if callback is None:
            return
        maybe_awaitable = callback(event)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable
