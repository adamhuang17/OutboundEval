"""Prompt builders for the staged LLM task compiler.

The compiler intentionally sends only AST slices that are relevant to the
current stage. This keeps provider gateways away from the "large prompt +
large schema + strict structured output" failure mode.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from outbound_eval.compiler.markdown_ast import MarkdownAstParser
from outbound_eval.domain.schemas_markdown import MarkdownAst, MarkdownNode


STAGE_TASK_OUTLINE = "task_outline"
STAGE_FLOW_COMPILE = "flow_compile"
STAGE_KNOWLEDGE_COMPILE = "knowledge_compile"
STAGE_CONSTRAINT_COMPILE = "constraint_compile"
STAGE_REQUIREMENT_SYNTH = "requirement_synth"
STAGE_JUDGE_PLAN_BUILD = "judge_plan_build"


@dataclass(frozen=True)
class StagePromptSpec:
    stage: str
    title: str
    system: str
    user_task: str
    schema_hint: dict
    include_keywords: tuple[str, ...]
    max_nodes: int
    max_chars: int


_COMMON_RULES = """You are a general outbound task instruction compiler.
Rules:
- Use only the supplied Markdown AST nodes and prior stage artifacts.
- Do not invent policy, money amounts, process steps, or promises absent from source.
- Every extracted artifact must include source_node_id from the supplied node list.
- Return exactly one JSON object and no Markdown/code fences.
"""


_STAGE_SPECS: dict[str, StagePromptSpec] = {
    STAGE_TASK_OUTLINE: StagePromptSpec(
        stage=STAGE_TASK_OUTLINE,
        title="Extract task outline",
        system=_COMMON_RULES,
        user_task=(
            "Extract only task_name, role, objective, opening_line, and section_intents. "
            "section_intents should map source_node_id to one of: role, objective, opening, "
            "flow, knowledge, constraint, termination, risk, other."
        ),
        schema_hint={
            "task_name": "short string",
            "role": "string",
            "objective": "string",
            "opening_line": "string",
            "section_intents": [{"source_node_id": "node_0001", "intent": "flow", "reason": "string"}],
            "compile_findings": [],
            "compiler_notes": [],
        },
        include_keywords=(),
        max_nodes=80,
        max_chars=16000,
    ),
    STAGE_FLOW_COMPILE: StagePromptSpec(
        stage=STAGE_FLOW_COMPILE,
        title="Compile flow and branches",
        system=_COMMON_RULES,
        user_task=(
            "Extract only call flow steps and branching rules. Ignore FAQ details and scoring criteria "
            "unless they explicitly describe process or branch conditions."
        ),
        schema_hint={
            "flow_nodes": [
                {
                    "id": "flow.001",
                    "name": "string",
                    "instruction": "string",
                    "requirement_ids": [],
                    "order": 0,
                    "source_node_id": "node_0001",
                    "source_text": "short quote",
                }
            ],
            "branch_rules": [
                {
                    "id": "branch.001",
                    "name": "string",
                    "condition": "string",
                    "source_node_id": "node_0001",
                    "source_text": "short quote",
                }
            ],
            "compile_findings": [],
            "compiler_notes": [],
        },
        include_keywords=(
            "flow",
            "process",
            "step",
            "script",
            "dialogue",
            "conversation",
            "branch",
            "流程",
            "步骤",
            "话术",
            "分支",
            "环节",
            "引导",
        ),
        max_nodes=32,
        max_chars=10000,
    ),
    STAGE_KNOWLEDGE_COMPILE: StagePromptSpec(
        stage=STAGE_KNOWLEDGE_COMPILE,
        title="Compile knowledge and FAQ",
        system=_COMMON_RULES,
        user_task=(
            "Extract only FAQ, knowledge points, business facts, definitions, policy facts, "
            "and answerable user questions. Do not extract generic process steps."
        ),
        schema_hint={
            "knowledge_facts": [
                {
                    "id": "kf.001",
                    "text": "fact or FAQ question",
                    "fact_type": "faq|policy|business_rule|procedure|definition|constraint_detail|other",
                    "source_node_id": "node_0001",
                    "source_text": "short quote",
                    "requirement_ids": [],
                    "question_patterns": [],
                    "answer": "string or null",
                }
            ],
            "compile_findings": [],
            "compiler_notes": [],
        },
        include_keywords=(
            "knowledge",
            "faq",
            "q&a",
            "question",
            "answer",
            "policy",
            "fact",
            "definition",
            "知识",
            "常见问题",
            "问答",
            "问题",
            "答案",
            "政策",
            "规则",
            "业务",
            "口径",
        ),
        max_nodes=40,
        max_chars=12000,
    ),
    STAGE_CONSTRAINT_COMPILE: StagePromptSpec(
        stage=STAGE_CONSTRAINT_COMPILE,
        title="Compile constraints and stop rules",
        system=_COMMON_RULES,
        user_task=(
            "Extract only constraints, forbidden behaviors, safety/compliance limits, "
            "termination rules, and risks that need coverage."
        ),
        schema_hint={
            "constraints": [
                {
                    "id": "con.001",
                    "name": "string",
                    "rule_text": "string",
                    "severity": "critical|major|minor",
                    "source_node_id": "node_0001",
                    "source_text": "short quote",
                }
            ],
            "forbidden_behaviors": [
                {
                    "id": "fb.001",
                    "name": "string",
                    "description": "string",
                    "severity": "critical|major|minor",
                    "cap_score": 60.0,
                    "source_node_id": "node_0001",
                    "source_text": "short quote",
                }
            ],
            "termination_rules": [
                {
                    "id": "term.001",
                    "name": "string",
                    "condition": "string",
                    "source_node_id": "node_0001",
                    "source_text": "short quote",
                }
            ],
            "risk_plan": {"detected_risks": [], "coverage_requirements": []},
            "compile_findings": [],
            "compiler_notes": [],
        },
        include_keywords=(
            "constraint",
            "forbidden",
            "ban",
            "must not",
            "stop",
            "termination",
            "risk",
            "compliance",
            "safety",
            "约束",
            "禁止",
            "不得",
            "不能",
            "不允许",
            "终止",
            "结束",
            "风险",
            "安全",
            "合规",
        ),
        max_nodes=40,
        max_chars=12000,
    ),
    STAGE_REQUIREMENT_SYNTH: StagePromptSpec(
        stage=STAGE_REQUIREMENT_SYNTH,
        title="Synthesize requirements",
        system=_COMMON_RULES,
        user_task=(
            "Synthesize atomic requirements from prior stage artifacts. Link each requirement "
            "to the strongest source_node_id. Do not add new business facts."
        ),
        schema_hint={
            "requirements": [
                {
                    "id": "req.001",
                    "name": "string",
                    "category": "task|flow|knowledge|constraint|exception|termination",
                    "source_node_id": "node_0001",
                    "source_text": "short quote",
                    "check_method": "rule|flow|knowledge|llm|hybrid",
                    "severity": "critical|major|minor",
                    "tags": [],
                }
            ],
            "compile_findings": [],
            "compiler_notes": [],
        },
        include_keywords=(),
        max_nodes=20,
        max_chars=10000,
    ),
    STAGE_JUDGE_PLAN_BUILD: StagePromptSpec(
        stage=STAGE_JUDGE_PLAN_BUILD,
        title="Build judge plan",
        system=_COMMON_RULES,
        user_task=(
            "Build judge points from the synthesized requirements and prior artifacts. Cover "
            "task_completion, flow_following, knowledge_correctness, constraint_following, "
            "exception_handling, user_experience, and safety_compliance when supported by source."
        ),
        schema_hint={
            "judge_plan": {
                "judge_points": [
                    {
                        "id": "jp.001",
                        "dimension": "task_completion|flow_following|knowledge_correctness|constraint_following|exception_handling|user_experience|safety_compliance",
                        "criterion": "string",
                        "pass_criteria": "string",
                        "partial_criteria": "string",
                        "fail_criteria": "string",
                        "severity": "critical|major|minor",
                        "weight": 1.0,
                        "source_node_id": "node_0001",
                        "source_text": "short quote",
                        "linked_requirement_ids": ["req.001"],
                        "evaluator": "rule|llm|hybrid",
                    }
                ],
                "dimension_weights": {},
            },
            "compile_findings": [],
            "compiler_notes": [],
        },
        include_keywords=(),
        max_nodes=20,
        max_chars=10000,
    ),
}


def _node_text(node: MarkdownNode, max_chars: int) -> str:
    text = (node.raw_text or node.body or node.heading or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _node_matches(node: MarkdownNode, keywords: Iterable[str]) -> bool:
    haystack = " ".join(
        [
            node.heading or "",
            node.normalized_heading or "",
            " ".join(node.path or []),
            node.body or "",
        ]
    ).lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def select_stage_nodes(ast: MarkdownAst, stage: str) -> list[MarkdownNode]:
    """Select the AST slice relevant to a compile stage."""
    spec = _STAGE_SPECS[stage]
    parser = MarkdownAstParser()
    nodes = parser.flatten(ast)
    if not nodes and ast.root:
        nodes = [ast.root]
    if not spec.include_keywords:
        return nodes[: spec.max_nodes]

    selected = [node for node in nodes if _node_matches(node, spec.include_keywords)]
    if selected:
        return selected[: spec.max_nodes]
    # Fallback to concise global context when headings are not explicit.
    return nodes[: min(spec.max_nodes, 12)]


def build_node_payload(nodes: list[MarkdownNode], *, max_chars: int) -> list[dict]:
    per_node_budget = max(500, max_chars // max(1, len(nodes)))
    payload = []
    used = 0
    for node in nodes:
        remaining = max(0, max_chars - used)
        if remaining <= 0:
            break
        text = _node_text(node, min(per_node_budget, remaining))
        used += len(text)
        payload.append(
            {
                "id": node.id,
                "heading": node.heading,
                "path": node.path,
                "level": node.level,
                "lines": [node.start_line, node.end_line],
                "bullet_count": len(node.bullets),
                "text": text,
            }
        )
    return payload


def build_stage_messages(
    *,
    stage: str,
    ast: MarkdownAst,
    prior_artifacts: dict | None = None,
) -> list[dict[str, str]]:
    if stage not in _STAGE_SPECS:
        raise ValueError(f"unknown compile stage {stage}")
    spec = _STAGE_SPECS[stage]
    nodes = select_stage_nodes(ast, stage)
    node_payload = build_node_payload(nodes, max_chars=spec.max_chars)
    payload = {
        "stage": stage,
        "task": spec.user_task,
        "nodes": node_payload,
        "prior_artifacts": prior_artifacts or {},
        "output_schema_hint": spec.schema_hint,
    }
    return [
        {"role": "system", "content": spec.system},
        {
            "role": "user",
            "content": (
                f"{spec.title}\n\n"
                "Return JSON matching output_schema_hint. Input payload:\n"
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
            ),
        },
    ]


def build_compiler_messages(raw_markdown: str, ast: MarkdownAst) -> list[dict[str, str]]:
    """Legacy one-shot prompt entry kept for compatibility."""
    parser = MarkdownAstParser()
    nodes_flat = parser.flatten(ast)
    node_payload = build_node_payload(nodes_flat, max_chars=18000)
    payload = {
        "nodes": node_payload,
        "raw_markdown_excerpt": raw_markdown[:8000],
        "output": "full compiler draft JSON",
    }
    return [
        {"role": "system", "content": _COMMON_RULES},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]
