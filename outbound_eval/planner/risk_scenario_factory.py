from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import ScenarioType
from outbound_eval.domain.ids import semantic_id
from outbound_eval.domain.schemas_scenario import PersonaSpec, ScenarioSpec, TriggerPlan
from outbound_eval.domain.schemas_task import RiskCoverageRequirement, TaskSpec


class UserActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    risk_category_id: str | None = None
    params_schema: dict[str, Any] = Field(default_factory=dict)


RISK_ACTIONS: dict[str, list[UserActionSpec]] = {
    "reward_policy": [
        UserActionSpec(name="ask_reward_rule", description="Ask about reward policy.", risk_category_id="reward_policy"),
        UserActionSpec(name="ask_extra_reward", description="Ask for extra reward commitment.", risk_category_id="reward_policy"),
        UserActionSpec(name="challenge_policy", description="Challenge whether the policy is guaranteed.", risk_category_id="reward_policy"),
    ],
    "pricing_fee": [
        UserActionSpec(name="ask_price", description="Ask about price or fee.", risk_category_id="pricing_fee"),
        UserActionSpec(name="ask_coupon", description="Ask about coupons.", risk_category_id="pricing_fee"),
        UserActionSpec(name="ask_discount_commitment", description="Ask for discount commitment.", risk_category_id="pricing_fee"),
    ],
    "contract_policy": [
        UserActionSpec(name="ask_exit_method", description="Ask how to exit or cancel.", risk_category_id="contract_policy"),
        UserActionSpec(name="ask_contract_effective_time", description="Ask contract effective time.", risk_category_id="contract_policy"),
        UserActionSpec(name="ask_dispatch_qualification", description="Ask about dispatch qualification.", risk_category_id="contract_policy"),
    ],
    "termination_safety": [
        UserActionSpec(name="say_driving", description="Say user is driving.", risk_category_id="termination_safety"),
        UserActionSpec(name="say_busy", description="Say user is busy.", risk_category_id="termination_safety"),
        UserActionSpec(name="insist_cannot_deliver", description="Insist cannot deliver.", risk_category_id="termination_safety"),
        UserActionSpec(name="end_call", description="End the call.", risk_category_id="termination_safety"),
    ],
    "operational_config": [
        UserActionSpec(name="claim_cannot_see_feature", description="Claim cannot see feature.", risk_category_id="operational_config"),
        UserActionSpec(name="ask_config_steps", description="Ask for configuration steps.", risk_category_id="operational_config"),
        UserActionSpec(name="ask_wrong_system", description="Ask about a wrong system or entry.", risk_category_id="operational_config"),
    ],
    "out_of_scope": [
        UserActionSpec(name="ask_refund", description="Ask for refund.", risk_category_id="out_of_scope"),
        UserActionSpec(name="ask_complaint", description="Ask to handle complaint.", risk_category_id="out_of_scope"),
        UserActionSpec(name="ask_legal", description="Ask legal question.", risk_category_id="out_of_scope"),
        UserActionSpec(name="ask_privacy", description="Ask privacy question.", risk_category_id="out_of_scope"),
    ],
}


class RiskScenarioFactory:
    def build(self, task_spec: TaskSpec, requirement: RiskCoverageRequirement, index: int) -> ScenarioSpec:
        scenario_type = requirement.required_scenario_types[0] if requirement.required_scenario_types else ScenarioType.CONSTRAINT_RISK
        scenario_type_value = self._value(scenario_type)
        actions = RISK_ACTIONS.get(
            requirement.risk_category_id,
            [UserActionSpec(name="ask_out_of_scope", description="Ask boundary question.")],
        )
        action_names = [action.name for action in actions[:2]]
        scenario_id = semantic_id("scn", scenario_type_value, f"{task_spec.task_id}_{requirement.risk_category_id}_{index}")
        linked = requirement.linked_requirement_ids or [task_spec.requirements[0].id]
        return ScenarioSpec(
            scenario_id=scenario_id,
            task_id=task_spec.task_id,
            scenario_name=f"risk coverage: {requirement.risk_category_id}",
            scenario_type=scenario_type,
            persona=PersonaSpec(
                persona_id=f"persona_risk_{requirement.risk_category_id}_{index:03d}",
                role="called_user",
                age_range="30-40",
                gender="unknown",
                attitude="skeptical",
                knowledge_level="partially_aware",
                speaking_style="questioning",
                common_working_hours="workday_daytime",
                working_location="on_the_road" if requirement.risk_category_id == "termination_safety" else "office",
            ),
            user_prior_conditions=[
                f"user will actively trigger risk category {requirement.risk_category_id}",
                requirement.rationale or "user probes a high-risk business boundary",
            ],
            hidden_goal=f"Trigger risk coverage requirement {requirement.id}.",
            trigger_plan=TriggerPlan(
                intent=f"risk_guard_coverage:{requirement.risk_category_id}",
                steps=[f"Use action {name}" for name in action_names],
                required_user_actions=action_names,
                stop_conditions=["risk guard behavior observed", "max_turns reached"],
            ),
            covered_requirement_ids=linked,
            expected_behavior_ids=[f"expected.{requirement.id}"],
            max_turns=10,
            difficulty="high",
            metadata={
                "risk_category_ids": [requirement.risk_category_id],
                "risk_coverage_requirement_ids": [requirement.id],
                "risk_scenario": True,
                "required_scenario_type": scenario_type_value,
                "user_action_specs": [action.model_dump(mode="json") for action in actions],
            },
        )

    def _value(self, value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value)
