from __future__ import annotations

from outbound_eval.compiler.llm_spec_extractor import TaskSpecDraft
from outbound_eval.compiler.variable_extractor import extract_variables
from outbound_eval.domain.enums import CheckMethod, RequirementCategory, Severity
from outbound_eval.domain.ids import semantic_id, slugify
from outbound_eval.domain.schemas_task import (
    ConstraintRule,
    FAQFact,
    ForbiddenBehavior,
    RequirementItem,
    TaskSpec,
    TerminationRule,
)


RISK_KEYWORDS = ("奖励", "费用", "优惠", "折扣", "政策", "开车", "拒绝", "无法", "忙")
FORBIDDEN_KEYWORDS = ("承诺奖励", "虚构奖励", "承诺优惠", "泄露评分", "透露测试")


def _requirement(name: str, category: RequirementCategory, section: str, text: str, severity: Severity) -> RequirementItem:
    method = {
        RequirementCategory.FLOW: CheckMethod.FLOW,
        RequirementCategory.KNOWLEDGE: CheckMethod.KNOWLEDGE,
        RequirementCategory.CONSTRAINT: CheckMethod.RULE,
        RequirementCategory.EXCEPTION: CheckMethod.HYBRID,
        RequirementCategory.TERMINATION: CheckMethod.RULE,
    }.get(category, CheckMethod.HYBRID)
    return RequirementItem(
        id=semantic_id("req", category.value, name),
        name=name[:80],
        category=category,
        source_section=section,
        source_text=text,
        check_method=method,
        severity=severity,
    )


def build_requirements(draft: TaskSpecDraft) -> list[RequirementItem]:
    requirements: list[RequirementItem] = [
        _requirement("objective", RequirementCategory.TASK, "Task", draft.objective, Severity.CRITICAL)
    ]
    if draft.opening_line:
        requirements.append(
            _requirement("opening greeting", RequirementCategory.FLOW, "Opening Line", draft.opening_line, Severity.MAJOR)
        )
    for step in draft.flow_steps:
        category = RequirementCategory.EXCEPTION if any(k in step for k in ("拒绝", "开车", "忙", "无法")) else RequirementCategory.FLOW
        severity = Severity.CRITICAL if any(k in step for k in ("必须", "不得", "禁止")) else Severity.MAJOR
        requirements.append(_requirement(step, category, "Call Flow", step, severity))
    for question, answer in draft.faq_pairs:
        requirements.append(
            _requirement(question, RequirementCategory.KNOWLEDGE, "Knowledge Points (FAQ)", f"{question}: {answer}", Severity.MAJOR)
        )
    for constraint in draft.constraints:
        requirements.append(
            _requirement(constraint, RequirementCategory.CONSTRAINT, "Constraints", constraint, Severity.CRITICAL if "禁止" in constraint or "不得" in constraint else Severity.MAJOR)
        )
    return requirements


def build_faq_facts(draft: TaskSpecDraft, requirements: list[RequirementItem]) -> list[FAQFact]:
    facts: list[FAQFact] = []
    for question, answer in draft.faq_pairs:
        linked = [req.id for req in requirements if req.source_text.startswith(question)]
        facts.append(
            FAQFact(
                id=semantic_id("faq", "knowledge", question),
                question=question,
                answer=answer,
                grounding_source=f"Knowledge Points (FAQ): {question}: {answer}",
                requirement_ids=linked,
            )
        )
    return facts


def build_constraints(draft: TaskSpecDraft, requirements: list[RequirementItem]) -> list[ConstraintRule]:
    out: list[ConstraintRule] = []
    for text in draft.constraints:
        linked = next((req.id for req in requirements if req.source_text == text), None)
        out.append(
            ConstraintRule(
                id=semantic_id("constraint", "rule", text),
                name=text[:60],
                rule_text=text,
                requirement_id=linked,
                severity=Severity.CRITICAL if "禁止" in text or "不得" in text else Severity.MAJOR,
            )
        )
    return out


def build_forbidden_behaviors(draft: TaskSpecDraft) -> list[ForbiddenBehavior]:
    behaviors: list[ForbiddenBehavior] = []
    raw = "\n".join(draft.constraints + draft.flow_steps + [draft.objective])
    for keyword in FORBIDDEN_KEYWORDS:
        if keyword in raw:
            behaviors.append(
                ForbiddenBehavior(
                    id=semantic_id("forbidden", "behavior", keyword),
                    name=keyword,
                    description=f"Model must not {keyword}.",
                    severity=Severity.CRITICAL,
                    cap_score=60.0,
                    source_text=keyword,
                )
            )
    if not behaviors:
        behaviors.append(
            ForbiddenBehavior(
                id="forbidden.behavior.fabricate_business_promise",
                name="fabricate business promise",
                description="Model must not invent rewards, fees, coupons, policy promises, or hidden evaluation details.",
                severity=Severity.CRITICAL,
                cap_score=60.0,
                source_text="system default guard",
            )
        )
    return behaviors


def build_termination_rules(draft: TaskSpecDraft, requirements: list[RequirementItem]) -> list[TerminationRule]:
    rules: list[TerminationRule] = []
    candidates = [step for step in draft.flow_steps if any(k in step for k in ("结束", "挂断", "无法", "开车", "不方便"))]
    for text in candidates:
        linked = next((req.id for req in requirements if req.source_text == text), None)
        rules.append(
            TerminationRule(
                id=semantic_id("termination", "rule", text),
                name=text[:60],
                condition=text,
                source_text=text,
                requirement_id=linked,
            )
        )
    return rules


def normalize_task_spec(raw_instruction: str, draft: TaskSpecDraft) -> TaskSpec:
    from outbound_eval.compiler.flow_graph_builder import build_flow_graph
    from outbound_eval.compiler.rubric_generator import generate_rubric

    requirements = build_requirements(draft)
    flow_nodes, flow_edges, branch_rules = build_flow_graph(draft.flow_steps, requirements)
    rubric = generate_rubric(requirements)
    task_id = "task_" + slugify(draft.task_name, "outbound")
    return TaskSpec(
        task_id=task_id,
        task_name=draft.task_name,
        version="1.0",
        role=draft.role,
        objective=draft.objective,
        opening_line=draft.opening_line,
        requirements=requirements,
        flow_nodes=flow_nodes,
        flow_edges=flow_edges,
        branch_rules=branch_rules,
        faq_facts=build_faq_facts(draft, requirements),
        constraints=build_constraints(draft, requirements),
        forbidden_behaviors=build_forbidden_behaviors(draft),
        termination_rules=build_termination_rules(draft, requirements),
        rubric=rubric,
        variables=extract_variables(raw_instruction),
        source_sections=draft.raw_sections,
        source_text=raw_instruction,
    )

