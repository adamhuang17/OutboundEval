from __future__ import annotations

from outbound_eval.domain.enums import RequirementCategory, RiskGuardType
from outbound_eval.domain.ids import semantic_id
from outbound_eval.domain.schemas_task import (
    DetectedRisk,
    RiskCategory,
    RiskGuardStatus,
    SeverityCap,
    TaskSpec,
)


FORBIDDEN_GUARDS = {
    RiskGuardType.FORBIDDEN_FABRICATION,
    RiskGuardType.FORBIDDEN_COMMITMENT,
    RiskGuardType.FORBIDDEN_WRONG_GUIDANCE,
    RiskGuardType.FORBIDDEN_OVERCLAIM,
}


class GuardContractEvaluator:
    def evaluate(
        self,
        task_spec: TaskSpec,
        detected_risk: DetectedRisk,
        category: RiskCategory,
    ) -> RiskGuardStatus:
        present: set[RiskGuardType] = set()
        linked_req_ids: set[str] = set(detected_risk.matched_requirement_ids)
        linked_faq_ids: set[str] = set(detected_risk.matched_faq_fact_ids)
        linked_forbidden_ids: set[str] = set()
        linked_rubric_ids: set[str] = set()
        linked_cap_ids: set[str] = set()

        faq_hits = self._faq_grounding_hits(task_spec, category)
        if faq_hits:
            present.add(RiskGuardType.FAQ_GROUNDING)
            linked_faq_ids.update(faq_hits)
            for fact in task_spec.faq_facts:
                if fact.id in faq_hits:
                    linked_req_ids.update(fact.requirement_ids)
            for fact in task_spec.knowledge_facts:
                if fact.id in faq_hits:
                    linked_req_ids.update(fact.requirement_ids)

        for req in task_spec.requirements:
            if req.id in detected_risk.matched_requirement_ids:
                linked_req_ids.add(req.id)
            if str(req.category) == RequirementCategory.KNOWLEDGE.value and self._text_hits(req.source_text, category):
                present.add(RiskGuardType.KNOWLEDGE_REQUIREMENT)
                linked_req_ids.add(req.id)
            if str(req.category) == RequirementCategory.FLOW.value and self._text_hits(req.source_text, category):
                present.add(RiskGuardType.FLOW_REQUIREMENT)
                linked_req_ids.add(req.id)
            if str(req.category) == RequirementCategory.EXCEPTION.value and self._text_hits(req.source_text, category):
                present.add(RiskGuardType.EXCEPTION_REQUIREMENT)
                linked_req_ids.add(req.id)
            if str(req.category) == RequirementCategory.CONSTRAINT.value and self._text_hits(req.source_text, category):
                present.add(RiskGuardType.CONSTRAINT_RULE)
                linked_req_ids.add(req.id)

        for constraint in task_spec.constraints:
            if self._text_hits(constraint.rule_text, category):
                present.add(RiskGuardType.CONSTRAINT_RULE)
                if constraint.requirement_id:
                    linked_req_ids.add(constraint.requirement_id)

        if any(self._text_hits("\n".join([rule.condition, rule.source_text]), category) for rule in task_spec.termination_rules):
            present.add(RiskGuardType.TERMINATION_RULE)

        for behavior in task_spec.forbidden_behaviors:
            text = "\n".join([behavior.name, behavior.description, behavior.source_text])
            if not self._behavior_relevant(behavior.id, text, category):
                continue
            linked_forbidden_ids.add(behavior.id)
            guard_key = behavior.id.lower()
            lowered = text.lower()
            if "forbidden_fabrication" in guard_key or "fabricat" in lowered or "invent" in lowered or "编造" in text or "虚构" in text:
                present.add(RiskGuardType.FORBIDDEN_FABRICATION)
            if "forbidden_commitment" in guard_key or "commit" in lowered or "promise" in lowered or "承诺" in text:
                present.add(RiskGuardType.FORBIDDEN_COMMITMENT)
            if "forbidden_wrong_guidance" in guard_key or "wrong guidance" in lowered or "错误引导" in text or "配置" in text:
                present.add(RiskGuardType.FORBIDDEN_WRONG_GUIDANCE)
            if "forbidden_overclaim" in guard_key or "overclaim" in lowered or "超范围" in text:
                present.add(RiskGuardType.FORBIDDEN_OVERCLAIM)
            if behavior.cap_score is not None and category.default_cap is not None:
                present.add(RiskGuardType.SEVERITY_CAP)

        linked_req_for_rubric = set(linked_req_ids)
        for rubric in task_spec.rubric:
            if linked_req_for_rubric & set(rubric.linked_requirement_ids) or self._text_hits(
                "\n".join([rubric.dimension, rubric.success_criteria, rubric.fail_criteria]), category
            ):
                present.add(RiskGuardType.RUBRIC_ITEM)
                linked_rubric_ids.add(rubric.rubric_id)

        for cap in task_spec.severity_caps:
            if cap.risk_category_id == category.id:
                present.add(RiskGuardType.SEVERITY_CAP)
                linked_cap_ids.add(cap.id)

        for req in task_spec.risk_coverage_requirements:
            if req.risk_category_id == category.id:
                present.add(RiskGuardType.COVERAGE_REQUIREMENT)

        missing = [guard for guard in category.required_guards if guard not in present]
        return RiskGuardStatus(
            risk_category_id=category.id,
            present_guards=sorted(present, key=lambda item: item.value),
            missing_guards=missing,
            linked_requirement_ids=sorted(linked_req_ids),
            linked_faq_fact_ids=sorted(linked_faq_ids),
            linked_forbidden_behavior_ids=sorted(linked_forbidden_ids),
            linked_rubric_ids=sorted(linked_rubric_ids),
            linked_severity_cap_ids=sorted(linked_cap_ids),
            is_guarded=not missing,
        )

    def ensure_severity_cap(
        self,
        task_spec: TaskSpec,
        detected_risk: DetectedRisk,
        category: RiskCategory,
        forbidden_ids: list[str],
    ) -> SeverityCap | None:
        if category.default_cap is None:
            return None
        existing = next((cap for cap in task_spec.severity_caps if cap.risk_category_id == category.id), None)
        if existing:
            return existing
        cap = SeverityCap(
            id=semantic_id("cap", category.id, "default"),
            risk_category_id=category.id,
            condition=f"Severe violation of guarded risk category {category.id}.",
            cap_score=category.default_cap,
            linked_forbidden_behavior_ids=forbidden_ids,
            source_text="auto generated from risk taxonomy default_cap",
        )
        task_spec.severity_caps.append(cap)
        return cap

    def _faq_grounding_hits(self, task_spec: TaskSpec, category: RiskCategory) -> set[str]:
        hits: set[str] = set()
        for fact in task_spec.faq_facts:
            text = "\n".join([fact.question, fact.answer, fact.grounding_source])
            if self._text_hits(text, category) and fact.grounding_source.strip():
                hits.add(fact.id)
        for fact in task_spec.knowledge_facts:
            text = "\n".join([fact.text, fact.answer or "", fact.source_text])
            if self._text_hits(text, category) and fact.source_text.strip():
                hits.add(fact.id)
        return hits

    def _behavior_relevant(self, behavior_id: str, text: str, category: RiskCategory) -> bool:
        lowered = text.lower()
        if category.id in behavior_id:
            return True
        if self._text_hits(text, category):
            return True
        if "system default guard" in lowered and FORBIDDEN_GUARDS & set(category.required_guards):
            return True
        return False

    def _text_hits(self, text: str, category: RiskCategory) -> bool:
        return any(term and term in text for term in category.terms)
