from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_understanding import (
    JudgePoint,
    PersonaSpec,
    RiskCoverageReq,
    ScenarioPlan,
    ScenarioSet,
    ScenarioSpec,
    TaskUnderstanding,
)


class ScenarioRepairReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repaired: bool = False
    actions: list[str] = Field(default_factory=list)
    added_scenario_ids: list[str] = Field(default_factory=list)
    updated_scenario_ids: list[str] = Field(default_factory=list)


class ScenarioRepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_set: ScenarioSet
    report: ScenarioRepairReport = Field(default_factory=ScenarioRepairReport)


class ScenarioRepairService:
    """Deterministically repair LLM scenario output before ScenarioQAGate."""

    def repair(
        self,
        understanding: TaskUnderstanding,
        scenario_set: ScenarioSet,
        *,
        plan: ScenarioPlan | None = None,
    ) -> ScenarioRepairResult:
        task_id = str(understanding.task_spec.get("task_id") or scenario_set.task_id or "task_unknown")
        req_ids = self._requirement_ids(understanding)
        judge_points = list(understanding.judge_plan.judge_points)
        jp_by_id = {jp.id: jp for jp in judge_points}
        risk_reqs = list(understanding.risk_plan.coverage_requirements)
        risk_ids = {req.id for req in risk_reqs}

        report = ScenarioRepairReport()
        plan_items = list(plan.items if plan else [])
        scenarios: list[ScenarioSpec] = []
        used_ids: set[str] = set()

        for index, scenario in enumerate(scenario_set.scenarios):
            plan_item = plan_items[index] if index < len(plan_items) else None
            updated = False
            scenario_id = scenario.scenario_id.strip() or self._scenario_id(task_id, f"existing-{index}", len(scenarios) + 1)
            if scenario_id in used_ids:
                scenario_id = self._scenario_id(task_id, f"{scenario_id}-{index}", len(scenarios) + 1)
                updated = True
                report.actions.append(f"renamed duplicate scenario id at index {index}")
            used_ids.add(scenario_id)

            linked_jp = self._dedupe_known(scenario.linked_judge_point_ids, jp_by_id)
            if not linked_jp and plan_item:
                linked_jp = self._dedupe_known(plan_item.linked_judge_point_ids, jp_by_id)
                updated = bool(linked_jp)
            covered_req = self._dedupe_known(scenario.covered_requirement_ids, req_ids)
            plan_req = self._dedupe_known(plan_item.linked_requirement_ids if plan_item else [], req_ids)
            inferred_req = self._requirements_for_judges(linked_jp, jp_by_id, req_ids)
            if not covered_req:
                covered_req = plan_req or inferred_req
                updated = updated or bool(covered_req)
            else:
                merged_req = self._dedupe([*covered_req, *plan_req, *inferred_req])
                updated = updated or merged_req != covered_req
                covered_req = merged_req

            metadata = dict(scenario.metadata or {})
            plan_risks = self._dedupe_known(plan_item.linked_risk_coverage_ids if plan_item else [], risk_ids)
            if plan_risks:
                existing_risks = [str(item) for item in metadata.get("risk_coverage_requirement_ids", []) or []]
                merged_risks = self._dedupe_known([*existing_risks, *plan_risks], risk_ids)
                if merged_risks != existing_risks:
                    metadata["risk_coverage_requirement_ids"] = merged_risks
                    updated = True

            filled = scenario.model_copy(
                update={
                    "scenario_id": scenario_id,
                    "task_id": task_id,
                    "title": scenario.title.strip() or (plan_item.title if plan_item else f"Scenario {index + 1}"),
                    "user_goal": scenario.user_goal.strip()
                    or self._goal_for(linked_jp, jp_by_id, understanding),
                    "hidden_user_goal": scenario.hidden_user_goal.strip()
                    or self._hidden_goal_for(linked_jp, jp_by_id),
                    "initial_user_utterance": scenario.initial_user_utterance.strip()
                    or self._initial_utterance(understanding),
                    "dialogue_direction": scenario.dialogue_direction
                    or self._dialogue_direction_for(linked_jp, jp_by_id),
                    "expected_model_behavior": scenario.expected_model_behavior
                    or self._expected_behavior_for(linked_jp, jp_by_id),
                    "forbidden_behavior": scenario.forbidden_behavior
                    or self._forbidden_behavior_for(linked_jp, jp_by_id),
                    "stop_conditions": scenario.stop_conditions or ["coverage point observed", "max_turns reached"],
                    "linked_judge_point_ids": linked_jp,
                    "covered_requirement_ids": covered_req,
                    "metadata": metadata,
                }
            )
            if updated or filled != scenario:
                self._mark_updated(report, filled.scenario_id)
            scenarios.append(filled)

        self._ensure_judge_point_coverage(understanding, scenarios, used_ids, req_ids, jp_by_id, report)
        self._ensure_risk_coverage(understanding, scenarios, used_ids, req_ids, jp_by_id, risk_reqs, report)

        repaired_set = ScenarioSet(task_id=task_id, scenarios=scenarios)
        report.repaired = bool(report.actions or report.added_scenario_ids or report.updated_scenario_ids)
        return ScenarioRepairResult(scenario_set=repaired_set, report=report)

    def _ensure_judge_point_coverage(
        self,
        understanding: TaskUnderstanding,
        scenarios: list[ScenarioSpec],
        used_ids: set[str],
        req_ids: set[str],
        jp_by_id: dict[str, JudgePoint],
        report: ScenarioRepairReport,
    ) -> None:
        coverage = self._judge_coverage(scenarios)
        for jp in understanding.judge_plan.judge_points:
            if coverage.get(jp.id):
                continue
            reqs = self._dedupe_known(jp.linked_requirement_ids, req_ids)
            target = self._best_existing_scenario(scenarios, reqs)
            if target is not None:
                target.linked_judge_point_ids = self._dedupe([*target.linked_judge_point_ids, jp.id])
                target.covered_requirement_ids = self._dedupe_known([*target.covered_requirement_ids, *reqs], req_ids)
                target.expected_model_behavior = target.expected_model_behavior or self._expected_behavior_for([jp.id], jp_by_id)
                target.forbidden_behavior = target.forbidden_behavior or self._forbidden_behavior_for([jp.id], jp_by_id)
                self._mark_updated(report, target.scenario_id)
                report.actions.append(f"linked missing judge point {jp.id} to {target.scenario_id}")
                continue
            scenario = self._fallback_scenario(
                understanding=understanding,
                used_ids=used_ids,
                ref=jp.id,
                title=f"Coverage repair: {jp.id}",
                scenario_type=self._scenario_type_for_judge(jp),
                judge_point_ids=[jp.id],
                requirement_ids=reqs,
                metadata={"repair_generated": True, "repair_reason": "judge_point_uncovered"},
            )
            scenarios.append(scenario)
            report.added_scenario_ids.append(scenario.scenario_id)
            report.actions.append(f"added scenario for missing judge point {jp.id}")

    def _ensure_risk_coverage(
        self,
        understanding: TaskUnderstanding,
        scenarios: list[ScenarioSpec],
        used_ids: set[str],
        req_ids: set[str],
        jp_by_id: dict[str, JudgePoint],
        risk_reqs: list[RiskCoverageReq],
        report: ScenarioRepairReport,
    ) -> None:
        for risk_req in risk_reqs:
            while len(self._risk_coverage(scenarios).get(risk_req.id, [])) < risk_req.min_scenarios:
                linked_req_ids = self._risk_requirement_links(understanding, risk_req.id, req_ids)
                target = self._best_existing_scenario(scenarios, linked_req_ids)
                if target is not None and risk_req.id not in (target.metadata.get("risk_coverage_requirement_ids", []) or []):
                    metadata = dict(target.metadata or {})
                    metadata["risk_coverage_requirement_ids"] = self._dedupe(
                        [*(metadata.get("risk_coverage_requirement_ids") or []), risk_req.id]
                    )
                    metadata["risk_category_ids"] = self._dedupe(
                        [*(metadata.get("risk_category_ids") or []), risk_req.linked_risk_category_id]
                    )
                    target.metadata = metadata
                    self._mark_updated(report, target.scenario_id)
                    report.actions.append(f"linked missing risk coverage {risk_req.id} to {target.scenario_id}")
                    continue
                judge_point_ids = self._risk_judge_points(understanding, linked_req_ids)
                if not judge_point_ids and understanding.judge_plan.judge_points:
                    judge_point_ids = [understanding.judge_plan.judge_points[0].id]
                requirement_ids = linked_req_ids or self._requirements_for_judges(judge_point_ids, jp_by_id, req_ids)
                scenario = self._fallback_scenario(
                    understanding=understanding,
                    used_ids=used_ids,
                    ref=risk_req.id,
                    title=f"Risk coverage repair: {risk_req.id}",
                    scenario_type=self._scenario_type_for_risk(risk_req),
                    judge_point_ids=judge_point_ids,
                    requirement_ids=requirement_ids,
                    metadata={
                        "repair_generated": True,
                        "repair_reason": "risk_coverage_underfilled",
                        "risk_coverage_requirement_ids": [risk_req.id],
                        "risk_category_ids": [risk_req.linked_risk_category_id],
                        "risk_scenario": True,
                    },
                )
                scenarios.append(scenario)
                report.added_scenario_ids.append(scenario.scenario_id)
                report.actions.append(f"added scenario for missing risk coverage {risk_req.id}")

    def _fallback_scenario(
        self,
        *,
        understanding: TaskUnderstanding,
        used_ids: set[str],
        ref: str,
        title: str,
        scenario_type: str,
        judge_point_ids: list[str],
        requirement_ids: list[str],
        metadata: dict[str, Any],
    ) -> ScenarioSpec:
        task_id = str(understanding.task_spec.get("task_id") or "task_unknown")
        scenario_id = self._unique_scenario_id(task_id, ref, len(used_ids) + 1, used_ids)
        jp_by_id = {jp.id: jp for jp in understanding.judge_plan.judge_points}
        return ScenarioSpec(
            scenario_id=scenario_id,
            task_id=task_id,
            title=title,
            scenario_type=scenario_type,
            persona=PersonaSpec(
                identity="called user",
                relationship_to_task="task recipient",
                motivation="needs a clear and accurate answer",
                attitude="questioning",
                communication_style="natural",
                initial_focus="complete the call goal",
                decision_rule="continue until the covered behavior is observable",
                inconvenience_context="limited time",
            ),
            user_goal=self._goal_for(judge_point_ids, jp_by_id, understanding),
            hidden_user_goal=self._hidden_goal_for(judge_point_ids, jp_by_id),
            initial_user_utterance=self._initial_utterance(understanding),
            dialogue_direction=self._dialogue_direction_for(judge_point_ids, jp_by_id),
            expected_model_behavior=self._expected_behavior_for(judge_point_ids, jp_by_id),
            forbidden_behavior=self._forbidden_behavior_for(judge_point_ids, jp_by_id),
            stop_conditions=["covered behavior observed", "max_turns reached"],
            linked_judge_point_ids=judge_point_ids,
            covered_requirement_ids=requirement_ids,
            max_turns=8,
            metadata=metadata,
        )

    def _best_existing_scenario(self, scenarios: list[ScenarioSpec], requirement_ids: list[str]) -> ScenarioSpec | None:
        if not scenarios:
            return None
        if requirement_ids:
            req_set = set(requirement_ids)
            matching = [scenario for scenario in scenarios if req_set & set(scenario.covered_requirement_ids)]
            if matching:
                return min(matching, key=lambda item: len(item.linked_judge_point_ids))
        sparse = [scenario for scenario in scenarios if not scenario.linked_judge_point_ids]
        if sparse:
            return sparse[0]
        return None

    def _requirement_ids(self, understanding: TaskUnderstanding) -> set[str]:
        return {
            str(req.get("id"))
            for req in understanding.task_spec.get("requirements", []) or []
            if str(req.get("id", "")).strip()
        }

    def _risk_requirement_links(self, understanding: TaskUnderstanding, risk_req_id: str, req_ids: set[str]) -> list[str]:
        task_risk_reqs = understanding.task_spec.get("risk_coverage_requirements", []) or []
        for item in task_risk_reqs:
            if item.get("id") == risk_req_id:
                return self._dedupe_known(item.get("linked_requirement_ids", []) or [], req_ids)
        return []

    def _risk_judge_points(self, understanding: TaskUnderstanding, requirement_ids: list[str]) -> list[str]:
        req_set = set(requirement_ids)
        selected: list[str] = []
        for jp in understanding.judge_plan.judge_points:
            if req_set and req_set & set(jp.linked_requirement_ids):
                selected.append(jp.id)
        if selected:
            return self._dedupe(selected)
        for jp in understanding.judge_plan.judge_points:
            if jp.dimension in {"constraint_following", "exception_handling", "safety_compliance"}:
                selected.append(jp.id)
        return self._dedupe(selected[:1])

    def _judge_coverage(self, scenarios: list[ScenarioSpec]) -> dict[str, list[str]]:
        coverage: dict[str, list[str]] = {}
        for scenario in scenarios:
            for jp_id in scenario.linked_judge_point_ids:
                coverage.setdefault(jp_id, []).append(scenario.scenario_id)
        return coverage

    def _risk_coverage(self, scenarios: list[ScenarioSpec]) -> dict[str, list[str]]:
        coverage: dict[str, list[str]] = {}
        for scenario in scenarios:
            for risk_id in scenario.metadata.get("risk_coverage_requirement_ids", []) or []:
                coverage.setdefault(str(risk_id), []).append(scenario.scenario_id)
        return coverage

    def _requirements_for_judges(
        self,
        judge_point_ids: list[str],
        jp_by_id: dict[str, JudgePoint],
        req_ids: set[str],
    ) -> list[str]:
        out: list[str] = []
        for jp_id in judge_point_ids:
            jp = jp_by_id.get(jp_id)
            if jp:
                out.extend(jp.linked_requirement_ids)
        return self._dedupe_known(out, req_ids)

    def _goal_for(
        self,
        judge_point_ids: list[str],
        jp_by_id: dict[str, JudgePoint],
        understanding: TaskUnderstanding,
    ) -> str:
        criteria = [jp_by_id[jp_id].criterion for jp_id in judge_point_ids if jp_id in jp_by_id]
        if criteria:
            return f"Ask about: {criteria[0][:160]}"
        objective = str(understanding.task_spec.get("objective") or "").strip()
        return objective or "Complete the outbound call task."

    def _hidden_goal_for(self, judge_point_ids: list[str], jp_by_id: dict[str, JudgePoint]) -> str:
        criteria = [jp_by_id[jp_id].criterion for jp_id in judge_point_ids if jp_id in jp_by_id]
        if criteria:
            return "Trigger evaluation of: " + "; ".join(criteria[:3])[:300]
        return "Trigger coverage for the repaired scenario."

    def _initial_utterance(self, understanding: TaskUnderstanding) -> str:
        objective = str(understanding.task_spec.get("objective") or "").strip()
        if objective:
            return f"Hello, I have a question about {objective[:120]}."
        return "Hello, I have a question about this call."

    def _dialogue_direction_for(self, judge_point_ids: list[str], jp_by_id: dict[str, JudgePoint]) -> list[str]:
        criteria = [jp_by_id[jp_id].criterion for jp_id in judge_point_ids if jp_id in jp_by_id]
        if not criteria:
            return ["Ask a task-relevant question", "Continue until the target behavior is observable"]
        return [f"Probe {criterion[:180]}" for criterion in criteria[:3]]

    def _expected_behavior_for(self, judge_point_ids: list[str], jp_by_id: dict[str, JudgePoint]) -> list[str]:
        behaviors = [jp_by_id[jp_id].pass_criteria for jp_id in judge_point_ids if jp_id in jp_by_id and jp_by_id[jp_id].pass_criteria]
        return behaviors or ["Respond according to the task instruction and evidence."]

    def _forbidden_behavior_for(self, judge_point_ids: list[str], jp_by_id: dict[str, JudgePoint]) -> list[str]:
        behaviors = [jp_by_id[jp_id].fail_criteria for jp_id in judge_point_ids if jp_id in jp_by_id and jp_by_id[jp_id].fail_criteria]
        return behaviors or ["Give unsupported or task-violating information."]

    def _scenario_type_for_judge(self, judge_point: JudgePoint) -> str:
        mapping = {
            "flow_following": "branch",
            "knowledge_correctness": "knowledge_probe",
            "constraint_following": "constraint_probe",
            "exception_handling": "exception",
            "safety_compliance": "adversarial",
        }
        return mapping.get(judge_point.dimension, "main_flow")

    def _scenario_type_for_risk(self, risk_req: RiskCoverageReq) -> str:
        category = risk_req.linked_risk_category_id.lower()
        if "termination" in category:
            return "exception"
        if "safety" in category or "reward" in category or "risk" in category:
            return "adversarial"
        return "constraint_probe"

    def _unique_scenario_id(self, task_id: str, ref: str, index: int, used_ids: set[str]) -> str:
        base = self._scenario_id(task_id, ref, index)
        candidate = base
        suffix = 2
        while candidate in used_ids:
            candidate = f"{base}.{suffix}"
            suffix += 1
        used_ids.add(candidate)
        return candidate

    def _scenario_id(self, task_id: str, ref: str, index: int) -> str:
        return f"scn.repair.{index:03d}.{stable_hash(task_id + ref)[:8]}"

    def _dedupe_known(self, values: list[Any], known: set[str] | dict[str, Any]) -> list[str]:
        known_set = set(known.keys()) if isinstance(known, dict) else set(known)
        return self._dedupe([str(item) for item in values if str(item) in known_set])

    def _dedupe(self, values: list[Any]) -> list[str]:
        out: list[str] = []
        for value in values:
            item = str(value).strip()
            if item and item not in out:
                out.append(item)
        return out

    def _mark_updated(self, report: ScenarioRepairReport, scenario_id: str) -> None:
        if scenario_id not in report.updated_scenario_ids:
            report.updated_scenario_ids.append(scenario_id)
