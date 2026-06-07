from __future__ import annotations

from outbound_eval.domain.enums import FindingDecision, FindingSource, RiskGuardType, Severity
from outbound_eval.domain.ids import semantic_id
from outbound_eval.domain.schemas_judge import SpecFinding
from outbound_eval.domain.schemas_task import (
    DetectedRisk,
    ForbiddenBehavior,
    RiskCategory,
    RiskCoverageRequirement,
    RiskGuardStatus,
    TaskSpec,
    TerminationRule,
)
from outbound_eval.spec_qa.guard_contract import GuardContractEvaluator
from outbound_eval.spec_qa.risk_detector import RiskDetector
from outbound_eval.spec_qa.risk_taxonomy import RiskTaxonomy


class RiskAuditor:
    def __init__(self, taxonomy: RiskTaxonomy | None = None):
        self.taxonomy = taxonomy or RiskTaxonomy.load()
        self.detector = RiskDetector(self.taxonomy)
        self.guard_evaluator = GuardContractEvaluator()

    async def audit(self, raw_instruction: str, task_spec: TaskSpec) -> list[SpecFinding]:
        findings: list[SpecFinding] = []
        detected_risks = self.detector.detect(raw_instruction, task_spec)
        task_spec.detected_risks = detected_risks
        guard_statuses: list[RiskGuardStatus] = []

        for risk in detected_risks:
            category = self.taxonomy.get(risk.risk_category_id)
            coverage_req = self._ensure_coverage_requirement(task_spec, risk, category)
            auto_fixed = self._try_auto_fix(raw_instruction, task_spec, risk, category)
            status = self.guard_evaluator.evaluate(task_spec, risk, category)
            guard_statuses.append(status)
            if status.is_guarded:
                if auto_fixed:
                    findings.append(self._auto_fix_finding(risk, category, auto_fixed, coverage_req))
                else:
                    findings.append(self._auto_guarded_finding(risk, category, status, coverage_req))
            else:
                findings.append(self._blocking_finding(risk, category, status, coverage_req))

        task_spec.risk_guard_statuses = guard_statuses
        return findings

    def _ensure_coverage_requirement(
        self,
        task_spec: TaskSpec,
        risk: DetectedRisk,
        category: RiskCategory,
    ) -> RiskCoverageRequirement:
        linked = self._linked_requirement_ids(task_spec, risk)
        existing = next((item for item in task_spec.risk_coverage_requirements if item.risk_category_id == category.id), None)
        if existing:
            merged = sorted({*existing.linked_requirement_ids, *linked})
            if merged != existing.linked_requirement_ids:
                existing.linked_requirement_ids = merged
            return existing
        req = RiskCoverageRequirement(
            id=f"riskcov.{category.id}",
            risk_category_id=category.id,
            required_scenario_types=category.required_scenario_types,
            linked_requirement_ids=linked,
            min_scenarios=1,
            priority=category.default_severity,
            rationale=f"Risk {category.id} detected from terms: {', '.join(risk.matched_terms)}",
        )
        task_spec.risk_coverage_requirements.append(req)
        return req

    def _linked_requirement_ids(self, task_spec: TaskSpec, risk: DetectedRisk) -> list[str]:
        linked: set[str] = set(risk.matched_requirement_ids)
        for fact in task_spec.faq_facts:
            if fact.id in risk.matched_faq_fact_ids:
                linked.update(fact.requirement_ids)
        for constraint in task_spec.constraints:
            if constraint.id in risk.matched_constraint_ids and constraint.requirement_id:
                linked.add(constraint.requirement_id)
        if not linked and task_spec.requirements:
            linked.add(task_spec.requirements[0].id)
        return sorted(linked)

    def _try_auto_fix(self, raw_instruction: str, task_spec: TaskSpec, risk: DetectedRisk, category: RiskCategory) -> list[str]:
        applied: list[str] = []
        required = set(category.required_guards)
        if RiskGuardType.TERMINATION_RULE in required:
            before = {rule.id for rule in task_spec.termination_rules}
            rule = self._ensure_termination_rule(raw_instruction, task_spec, risk, category)
            if rule and rule.id not in before:
                applied.append(RiskGuardType.TERMINATION_RULE.value)

        forbidden_ids: list[str] = []
        forbidden_map = {
            RiskGuardType.FORBIDDEN_FABRICATION: "forbidden_fabrication",
            RiskGuardType.FORBIDDEN_COMMITMENT: "forbidden_commitment",
            RiskGuardType.FORBIDDEN_WRONG_GUIDANCE: "forbidden_wrong_guidance",
            RiskGuardType.FORBIDDEN_OVERCLAIM: "forbidden_overclaim",
        }
        for guard_type, guard_name in forbidden_map.items():
            if guard_type not in required:
                continue
            before = {item.id for item in task_spec.forbidden_behaviors}
            behavior = self._ensure_forbidden(task_spec, category, guard_name)
            forbidden_ids.append(behavior.id)
            if behavior.id not in before:
                applied.append(guard_type.value)

        if RiskGuardType.SEVERITY_CAP in required:
            before = {cap.id for cap in task_spec.severity_caps}
            cap = self.guard_evaluator.ensure_severity_cap(task_spec, risk, category, forbidden_ids)
            if cap and cap.id not in before:
                applied.append(RiskGuardType.SEVERITY_CAP.value)
        return applied

    def _ensure_termination_rule(
        self,
        raw_instruction: str,
        task_spec: TaskSpec,
        risk: DetectedRisk,
        category: RiskCategory,
    ) -> TerminationRule | None:
        existing = next(
            (
                rule
                for rule in task_spec.termination_rules
                if self._text_hits("\n".join([rule.condition, rule.source_text]), category)
            ),
            None,
        )
        if existing:
            return existing
        source = next((span for span in risk.source_spans if self._text_hits(span, category)), raw_instruction[:160])
        if not self._text_hits(source, category):
            return None
        rule = TerminationRule(
            id=semantic_id("termination", "risk", category.id),
            name=f"{category.id} termination rule",
            condition=source[:160],
            source_text=source,
            requirement_id=risk.matched_requirement_ids[0] if risk.matched_requirement_ids else None,
        )
        task_spec.termination_rules.append(rule)
        return rule

    def _ensure_forbidden(self, task_spec: TaskSpec, category: RiskCategory, guard_name: str) -> ForbiddenBehavior:
        existing = next(
            (
                item
                for item in task_spec.forbidden_behaviors
                if category.id in item.id
                or guard_name in item.id
                or self._text_hits("\n".join([item.name, item.description, item.source_text]), category)
            ),
            None,
        )
        if existing:
            return existing
        behavior = ForbiddenBehavior(
            id=f"forbidden.behavior.{category.id}.{guard_name}",
            name=f"{category.id} {guard_name}",
            description=f"Do not violate {category.semantic_description}",
            severity=category.default_severity,
            cap_score=category.default_cap,
            source_text="auto generated from risk taxonomy guard contract",
        )
        task_spec.forbidden_behaviors.append(behavior)
        return behavior

    def _auto_fix_finding(
        self,
        risk: DetectedRisk,
        category: RiskCategory,
        applied_guards: list[str],
        coverage_req: RiskCoverageRequirement,
    ) -> SpecFinding:
        return SpecFinding(
            id=f"risk.{category.id}.auto_fix",
            source=FindingSource.RISK,
            severity=Severity.MAJOR,
            requirement_ref=f"risk.{category.id}",
            detail=f"Risk category {category.id} received auto-generated guards.",
            suggested_fix="Review generated guards if business policy wording needs stricter phrasing.",
            decision=FindingDecision.AUTO_FIX,
            blocking=False,
            metadata={
                "risk_category": category.id,
                "applied_guards": applied_guards,
                "coverage_requirement_id": coverage_req.id,
                "matched_terms": risk.matched_terms,
                "present_guards": applied_guards,
                "missing_guards": [],
            },
        )

    def _auto_guarded_finding(
        self,
        risk: DetectedRisk,
        category: RiskCategory,
        status: RiskGuardStatus,
        coverage_req: RiskCoverageRequirement,
    ) -> SpecFinding:
        return SpecFinding(
            id=f"risk.{category.id}.guarded",
            source=FindingSource.RISK,
            severity=category.default_severity if category.default_severity != Severity.CRITICAL else Severity.MAJOR,
            requirement_ref=f"risk.{category.id}",
            detail=f"Risk category {category.id} detected and guarded.",
            suggested_fix="Coverage Planner must generate at least one risk scenario.",
            decision=FindingDecision.AUTO_GUARDED,
            blocking=False,
            metadata=self._metadata(risk, category, status, coverage_req),
        )

    def _blocking_finding(
        self,
        risk: DetectedRisk,
        category: RiskCategory,
        status: RiskGuardStatus,
        coverage_req: RiskCoverageRequirement,
    ) -> SpecFinding:
        missing = [self._value(item) for item in status.missing_guards]
        return SpecFinding(
            id=f"risk.{category.id}.missing_guards",
            source=FindingSource.RISK,
            severity=category.default_severity,
            requirement_ref=f"risk.{category.id}",
            detail=f"Risk category {category.id} detected but missing required guards.",
            suggested_fix=f"Add missing guards: {', '.join(missing)}.",
            decision=FindingDecision.HUMAN_NEEDED,
            blocking=True,
            metadata=self._metadata(risk, category, status, coverage_req),
        )

    def _metadata(
        self,
        risk: DetectedRisk,
        category: RiskCategory,
        status: RiskGuardStatus,
        coverage_req: RiskCoverageRequirement,
    ) -> dict:
        return {
            "risk_category": category.id,
            "risk_name": category.name,
            "present_guards": [self._value(item) for item in status.present_guards],
            "missing_guards": [self._value(item) for item in status.missing_guards],
            "coverage_requirement_id": coverage_req.id,
            "matched_terms": risk.matched_terms,
            "linked_requirement_ids": status.linked_requirement_ids,
            "linked_faq_fact_ids": status.linked_faq_fact_ids,
            "linked_forbidden_behavior_ids": status.linked_forbidden_behavior_ids,
            "linked_rubric_ids": status.linked_rubric_ids,
            "linked_severity_cap_ids": status.linked_severity_cap_ids,
        }

    def _text_hits(self, text: str, category: RiskCategory) -> bool:
        return any(term and term in text for term in category.terms)

    def _value(self, item) -> str:
        return item.value if hasattr(item, "value") else str(item)
