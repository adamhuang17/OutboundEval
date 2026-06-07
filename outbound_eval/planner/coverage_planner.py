from __future__ import annotations

from itertools import cycle
from typing import Any

from outbound_eval.domain.enums import RequirementCategory, ScenarioType, Severity
from outbound_eval.domain.ids import semantic_id, slugify
from outbound_eval.domain.schemas_scenario import CoverageMatrix, PersonaSpec, ScenarioSpec, TriggerPlan
from outbound_eval.domain.schemas_task import RequirementItem, RiskCoverageRequirement, TaskSpec
from outbound_eval.planner.risk_scenario_factory import RiskScenarioFactory


DEFAULT_SCENARIO_BLUEPRINTS = [
    (ScenarioType.HAPPY_PATH, "normal confirmation", ["user is cooperative", "user can confirm the flow"], ["answer_yes"]),
    (ScenarioType.MAIN_FLOW, "main flow detail check", ["user asks one operational detail"], ["answer_yes", "ask_faq"]),
    (ScenarioType.MAIN_FLOW, "short main flow", ["user is busy but willing to cooperate"], ["say_busy", "answer_yes"]),
    (ScenarioType.MAIN_FLOW, "repeat key point", ["user asks the model to repeat the key point"], ["ask_faq"]),
    (ScenarioType.EXCEPTION, "user refusal", ["user does not want to continue", "user clearly refuses"], ["refuse"]),
    (ScenarioType.EXCEPTION, "driving or unavailable", ["user is driving", "user cannot continue the call"], ["say_driving", "end_call"]),
    (ScenarioType.EXCEPTION, "wrong responsible person", ["user is not the responsible person"], ["claim_not_responsible"]),
    (ScenarioType.FAQ_PROBE, "key faq probe", ["user asks about fee, time, quantity, or policy"], ["ask_faq"]),
    (ScenarioType.FAQ_PROBE, "knowledge detail probe", ["user does not understand business terms"], ["ask_faq"]),
    (ScenarioType.CONSTRAINT_RISK, "boundary question", ["user asks for discounts, rewards, or extra commitment"], ["ask_out_of_scope"]),
    (ScenarioType.CONSTRAINT_RISK, "cannot see config", ["user cannot see the related entry or cannot perform an action"], ["claim_cannot_see_feature"]),
    (ScenarioType.METAMORPHIC, "paraphrased flow", ["user changes expression order", "user emotion fluctuates slightly"], ["interrupt", "ask_faq"]),
]


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
        blueprints = self._expand_blueprints(budget)
        high_value = self._prioritized_requirements(task_spec)
        assignments = self._assign_requirements(high_value, len(blueprints))
        scenarios: list[ScenarioSpec] = []
        for index, blueprint in enumerate(blueprints):
            scenario_type, name, prior_conditions, actions = blueprint
            covered = assignments[index] or [high_value[index % len(high_value)].id]
            scenarios.append(self._scenario(task_spec, scenario_type, name, index + 1, prior_conditions, actions, covered))
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

    def _expand_blueprints(self, budget: int) -> list[tuple[ScenarioType, str, list[str], list[str]]]:
        if budget <= len(DEFAULT_SCENARIO_BLUEPRINTS):
            return DEFAULT_SCENARIO_BLUEPRINTS[:budget]
        out = list(DEFAULT_SCENARIO_BLUEPRINTS)
        extra_types = [ScenarioType.BRANCH, ScenarioType.FAQ_PROBE, ScenarioType.CONSTRAINT_RISK, ScenarioType.ADVERSARIAL]
        while len(out) < budget:
            scenario_type = extra_types[len(out) % len(extra_types)]
            out.append((scenario_type, f"supplemental coverage {len(out) + 1}", ["user triggers an uncovered path"], ["ask_faq"]))
        return out

    def _prioritized_requirements(self, task_spec: TaskSpec) -> list[RequirementItem]:
        requirements = list(task_spec.requirements)
        requirements.sort(key=lambda req: (0 if str(req.severity) == Severity.CRITICAL.value else 1, str(req.category), req.id))
        return requirements or task_spec.requirements

    def _assign_requirements(self, requirements: list[RequirementItem], slots: int) -> list[list[str]]:
        assignments = [[] for _ in range(slots)]
        for index, req in enumerate(requirements):
            assignments[index % slots].append(req.id)
        if requirements:
            req_cycle = cycle(requirements)
            for bucket in assignments:
                if not bucket:
                    bucket.append(next(req_cycle).id)
        return assignments

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
        scenario_type: ScenarioType,
        name: str,
        index: int,
        prior_conditions: list[str],
        actions: list[str],
        covered_requirement_ids: list[str],
    ) -> ScenarioSpec:
        scenario_type_value = self._value(scenario_type)
        scenario_id = semantic_id("scn", scenario_type_value, f"{task_spec.task_id}_{index}_{name}")
        return ScenarioSpec(
            scenario_id=scenario_id,
            task_id=task_spec.task_id,
            scenario_name=name,
            scenario_type=scenario_type,
            persona=self._persona(scenario_type, index),
            user_prior_conditions=prior_conditions,
            hidden_goal=f"Trigger and evaluate: {', '.join(covered_requirement_ids)}",
            trigger_plan=TriggerPlan(
                intent=name,
                steps=[f"Use action {action}" for action in actions],
                required_user_actions=actions,
                stop_conditions=["covered requirements triggered", "max_turns reached"],
            ),
            covered_requirement_ids=covered_requirement_ids,
            expected_behavior_ids=[f"expected.{slugify(rid, 'req')}" for rid in covered_requirement_ids],
            max_turns=10,
            difficulty="high" if scenario_type_value in {ScenarioType.CONSTRAINT_RISK.value, ScenarioType.ADVERSARIAL.value} else "medium",
        )

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
