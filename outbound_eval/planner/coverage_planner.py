from __future__ import annotations

from typing import Any

from outbound_eval.domain.enums import RequirementCategory, ScenarioType, Severity
from outbound_eval.domain.ids import semantic_id, slugify
from outbound_eval.domain.schemas_scenario import CoverageMatrix, PersonaSpec, ScenarioSpec, TriggerPlan
from outbound_eval.domain.schemas_task import RequirementItem, RiskCoverageRequirement, TaskSpec
from outbound_eval.planner.risk_scenario_factory import RiskScenarioFactory


class CoveragePlanner:
    def __init__(self, apply_coverage_qa: bool = True, risk_factory: RiskScenarioFactory | None = None):
        self.apply_coverage_qa = apply_coverage_qa
        self.risk_factory = risk_factory or RiskScenarioFactory()

    def plan(self, task_spec: TaskSpec, budget: int = 12) -> CoverageMatrix:
        if budget not in {8, 12, 20}:
            raise ValueError("budget must be 8, 12, or 20")
        base_scenarios = self._generate_base_scenarios(task_spec, budget)
        scenarios = self._ensure_risk_coverage(task_spec, base_scenarios)
        scenarios = self._rank_and_fit_budget(task_spec, scenarios, budget)
        matrix = self._coverage_matrix(task_spec, scenarios)
        if not self.apply_coverage_qa:
            return matrix
        from outbound_eval.planner.coverage_qa import CoverageQA

        return CoverageQA(self.risk_factory).validate_or_autofill(task_spec, matrix, budget)

    def _generate_base_scenarios(self, task_spec: TaskSpec, budget: int) -> list[ScenarioSpec]:
        high_value = self._prioritized_requirements(task_spec)
        scenarios: list[ScenarioSpec] = []
        if not high_value:
            return scenarios
        for index in range(budget):
            requirement = high_value[index % len(high_value)]
            scenarios.append(self._scenario(task_spec, requirement, index + 1))
        return scenarios

    def _ensure_risk_coverage(self, task_spec: TaskSpec, scenarios: list[ScenarioSpec]) -> list[ScenarioSpec]:
        out = list(scenarios)
        covered = self._risk_requirement_ids(out)
        for requirement in self._sorted_risk_requirements(task_spec.risk_coverage_requirements):
            existing_count = len(covered.get(requirement.id, []))
            while existing_count < requirement.min_scenarios:
                risk_scenario = self.risk_factory.build(task_spec, requirement, len(out) + 1)
                out.append(risk_scenario)
                covered.setdefault(requirement.id, []).append(risk_scenario.scenario_id)
                existing_count += 1
        return out

    def _rank_and_fit_budget(self, task_spec: TaskSpec, scenarios: list[ScenarioSpec], budget: int) -> list[ScenarioSpec]:
        deduped: dict[str, ScenarioSpec] = {}
        for scenario in scenarios:
            deduped.setdefault(scenario.scenario_id, scenario)
        ranked = sorted(deduped.values(), key=lambda item: self._scenario_priority(task_spec, item))
        return ranked[:budget]

    def _prioritized_requirements(self, task_spec: TaskSpec) -> list[RequirementItem]:
        requirements = list(task_spec.requirements)
        requirements.sort(key=lambda req: (0 if str(req.severity) == Severity.CRITICAL.value else 1, str(req.category), req.id))
        return requirements or task_spec.requirements

    def _persona(self, scenario_type: ScenarioType | str, index: int) -> PersonaSpec:
        scenario_type_value = self._value(scenario_type)
        attitude = {
            ScenarioType.HAPPY_PATH.value: "cooperative",
            ScenarioType.EXCEPTION.value: "busy",
            ScenarioType.CONSTRAINT_RISK.value: "skeptical",
            ScenarioType.ADVERSARIAL.value: "resistant",
            ScenarioType.METAMORPHIC.value: "questioning",
        }.get(scenario_type_value, "questioning")
        return PersonaSpec(
            persona_id=f"persona_{scenario_type_value}_{index:03d}",
            role="called_user",
            age_range="30-40",
            gender="unknown",
            attitude=attitude,
            knowledge_level="partially_aware" if scenario_type_value == ScenarioType.FAQ_PROBE.value else "unaware",
            speaking_style="interrupting" if scenario_type_value == ScenarioType.METAMORPHIC.value else "colloquial",
            common_working_hours="workday_daytime",
            working_location="on_the_road" if scenario_type_value == ScenarioType.EXCEPTION.value and index == 6 else "office",
        )

    def _scenario(
        self,
        task_spec: TaskSpec,
        requirement: RequirementItem,
        index: int,
    ) -> ScenarioSpec:
        scenario_type = self._scenario_type_for(requirement)
        scenario_type_value = self._value(scenario_type)
        name = f"{requirement.name} coverage"
        scenario_id = semantic_id("scn", scenario_type_value, f"{task_spec.task_id}_{index}_{requirement.id}_{requirement.name}")
        return ScenarioSpec(
            scenario_id=scenario_id,
            task_id=task_spec.task_id,
            scenario_name=name,
            scenario_type=scenario_type,
            persona=self._persona(scenario_type, index),
            user_prior_conditions=[f"User probes requirement {requirement.id}: {requirement.name}"],
            hidden_goal=f"Trigger and evaluate requirement {requirement.id}: {requirement.source_text[:160]}",
            trigger_plan=TriggerPlan(
                intent=name,
                steps=[f"Ask about or challenge requirement {requirement.id}", "Decide whether the answer satisfies the requirement"],
                required_user_actions=self._actions_for(requirement),
                stop_conditions=["covered requirements triggered", "max_turns reached"],
            ),
            covered_requirement_ids=[requirement.id],
            expected_behavior_ids=[f"expected.{slugify(requirement.id, 'req')}"],
            max_turns=10,
            difficulty="high" if scenario_type_value in {ScenarioType.CONSTRAINT_RISK.value, ScenarioType.ADVERSARIAL.value} else "medium",
        )

    def _scenario_type_for(self, requirement: RequirementItem) -> ScenarioType:
        category = self._value(requirement.category)
        if category == RequirementCategory.KNOWLEDGE.value:
            return ScenarioType.FAQ_PROBE
        if category == RequirementCategory.CONSTRAINT.value:
            return ScenarioType.CONSTRAINT_RISK
        if category in {RequirementCategory.EXCEPTION.value, RequirementCategory.TERMINATION.value}:
            return ScenarioType.EXCEPTION
        if category == RequirementCategory.FLOW.value:
            return ScenarioType.MAIN_FLOW
        return ScenarioType.MAIN_FLOW

    def _actions_for(self, requirement: RequirementItem) -> list[str]:
        category = self._value(requirement.category)
        if category == RequirementCategory.KNOWLEDGE.value:
            return ["ask_detail", "end_call"]
        if category == RequirementCategory.CONSTRAINT.value:
            return ["challenge_constraint", "end_call"]
        if category in {RequirementCategory.EXCEPTION.value, RequirementCategory.TERMINATION.value}:
            return ["refuse", "end_call"]
        return ["answer_yes", "ask_detail", "end_call"]

    def _coverage_matrix(self, task_spec: TaskSpec, scenarios: list[ScenarioSpec]) -> CoverageMatrix:
        req_coverage: dict[str, list[str]] = {}
        for scenario in scenarios:
            for req_id in scenario.covered_requirement_ids:
                req_coverage.setdefault(req_id, []).append(scenario.scenario_id)
        flow_coverage = {
            node.id: [
                scenario.scenario_id
                for scenario in scenarios
                if any(rid in scenario.covered_requirement_ids for rid in node.requirement_ids)
            ]
            for node in task_spec.flow_nodes
        }
        branch_coverage = {
            rule.id: [
                scenario.scenario_id
                for scenario in scenarios
                if rule.requirement_id in scenario.covered_requirement_ids or self._value(scenario.scenario_type) == ScenarioType.BRANCH.value
            ]
            for rule in task_spec.branch_rules
        }
        faq_coverage = {
            fact.id: [
                scenario.scenario_id
                for scenario in scenarios
                if set(scenario.covered_requirement_ids) & set(fact.requirement_ids)
            ]
            for fact in task_spec.faq_facts
        }
        faq_coverage.update(
            {
                fact.id: [
                    scenario.scenario_id
                    for scenario in scenarios
                    if set(scenario.covered_requirement_ids) & set(fact.requirement_ids)
                ]
                for fact in task_spec.knowledge_facts
            }
        )
        risk_req_ids = {
            req.id
            for req in task_spec.requirements
            if str(req.category) in {RequirementCategory.CONSTRAINT.value, RequirementCategory.EXCEPTION.value}
        }
        risk_coverage = {
            req_id: [scenario.scenario_id for scenario in scenarios if req_id in scenario.covered_requirement_ids]
            for req_id in risk_req_ids
        }
        risk_requirement_coverage = self._risk_requirement_ids(scenarios)
        uncovered_risk = [
            req.id
            for req in task_spec.risk_coverage_requirements
            if len(risk_requirement_coverage.get(req.id, [])) < req.min_scenarios
        ]
        return CoverageMatrix(
            task_id=task_spec.task_id,
            scenarios=scenarios,
            requirement_coverage={k: v for k, v in req_coverage.items() if v},
            flow_node_coverage={k: v for k, v in flow_coverage.items() if v},
            branch_coverage={k: v for k, v in branch_coverage.items() if v},
            faq_coverage={k: v for k, v in faq_coverage.items() if v},
            risk_coverage={k: v for k, v in risk_coverage.items() if v},
            risk_requirement_coverage={k: v for k, v in risk_requirement_coverage.items() if v},
            uncovered_requirement_ids=[req.id for req in task_spec.requirements if req.id not in req_coverage],
            uncovered_risk_coverage_requirement_ids=uncovered_risk,
        )

    def _risk_requirement_ids(self, scenarios: list[ScenarioSpec]) -> dict[str, list[str]]:
        coverage: dict[str, list[str]] = {}
        for scenario in scenarios:
            for req_id in scenario.metadata.get("risk_coverage_requirement_ids", []):
                coverage.setdefault(str(req_id), []).append(scenario.scenario_id)
        return coverage

    def _sorted_risk_requirements(self, requirements: list[RiskCoverageRequirement]) -> list[RiskCoverageRequirement]:
        return sorted(
            requirements,
            key=lambda req: (
                0 if str(req.priority) == Severity.CRITICAL.value else 1,
                0 if req.risk_category_id == "termination_safety" else 1,
                req.id,
            ),
        )

    def _scenario_priority(self, task_spec: TaskSpec, scenario: ScenarioSpec) -> tuple[int, str]:
        metadata = scenario.metadata or {}
        risk_req_ids = set(metadata.get("risk_coverage_requirement_ids", []))
        if metadata.get("risk_scenario"):
            reqs = [req for req in task_spec.risk_coverage_requirements if req.id in risk_req_ids]
            if any(str(req.priority) == Severity.CRITICAL.value for req in reqs):
                return (0, scenario.scenario_id)
            if any(req.risk_category_id == "termination_safety" for req in reqs):
                return (1, scenario.scenario_id)
            return (2, scenario.scenario_id)
        covered = set(scenario.covered_requirement_ids)
        if any(req.id in covered and str(req.severity) == Severity.CRITICAL.value for req in task_spec.requirements):
            return (3, scenario.scenario_id)
        scenario_type = self._value(scenario.scenario_type)
        if scenario_type == ScenarioType.FAQ_PROBE.value:
            return (4, scenario.scenario_id)
        if scenario_type == ScenarioType.BRANCH.value:
            return (5, scenario.scenario_id)
        if scenario_type == ScenarioType.MAIN_FLOW.value:
            return (6, scenario.scenario_id)
        if scenario_type == ScenarioType.METAMORPHIC.value:
            return (7, scenario.scenario_id)
        return (6, scenario.scenario_id)

    def _value(self, value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value)
