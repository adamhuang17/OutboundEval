from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, create_model


class EmptyActionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FAQActionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_hint: str = ""


class RegisteredUserAction(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    name: str
    description: str
    param_model: type[BaseModel]
    terminates_episode: bool = False
    applicable_intents: list[str] = Field(default_factory=list)

    def prompt_description(self) -> str:
        schema = self.param_model.model_json_schema()
        props = schema.get("properties", {})
        params = ", ".join(props.keys())
        return f"{self.name}: {self.description}" + (f" ({params})" if params else "")


class UserActionRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    actions: dict[str, RegisteredUserAction] = Field(default_factory=dict)

    def register(self, action: RegisteredUserAction) -> None:
        self.actions[action.name] = action

    def get(self, name: str) -> RegisteredUserAction:
        return self.actions[name]

    def prompt_description(self) -> str:
        return "\n".join(action.prompt_description() for action in self.actions.values())


def default_user_action_registry() -> UserActionRegistry:
    registry = UserActionRegistry()
    for name, desc, terminates, params in [
        ("answer_yes", "User agrees or confirms the requested information.", False, EmptyActionParams),
        ("answer_no", "User disagrees or corrects information.", False, EmptyActionParams),
        ("ask_faq", "User asks a knowledge or policy question.", False, FAQActionParams),
        ("refuse", "User refuses the request.", False, EmptyActionParams),
        ("say_busy", "User says they are busy.", False, EmptyActionParams),
        ("say_driving", "User says they are driving.", False, EmptyActionParams),
        ("interrupt", "User interrupts or changes topic.", False, EmptyActionParams),
        ("claim_not_responsible", "User says they are not responsible.", False, EmptyActionParams),
        ("claim_cannot_see_feature", "User says they cannot see the feature or cannot perform the action.", False, EmptyActionParams),
        ("ask_out_of_scope", "User asks for discounts, rewards, fees, or unrelated commitments.", False, FAQActionParams),
        ("ask_reward_rule", "User asks about reward policy.", False, FAQActionParams),
        ("ask_extra_reward", "User asks for extra reward commitment.", False, FAQActionParams),
        ("challenge_policy", "User challenges whether policy is guaranteed.", False, FAQActionParams),
        ("ask_price", "User asks about price or fee.", False, FAQActionParams),
        ("ask_coupon", "User asks about coupons.", False, FAQActionParams),
        ("ask_discount_commitment", "User asks for discount commitment.", False, FAQActionParams),
        ("ask_exit_method", "User asks how to exit or cancel.", False, FAQActionParams),
        ("ask_contract_effective_time", "User asks contract effective time.", False, FAQActionParams),
        ("ask_dispatch_qualification", "User asks about dispatch qualification.", False, FAQActionParams),
        ("insist_cannot_deliver", "User insists they cannot deliver.", False, EmptyActionParams),
        ("ask_config_steps", "User asks for configuration steps.", False, FAQActionParams),
        ("ask_wrong_system", "User asks about wrong system or entry.", False, FAQActionParams),
        ("ask_refund", "User asks for refund.", False, FAQActionParams),
        ("ask_complaint", "User asks to handle complaint.", False, FAQActionParams),
        ("ask_legal", "User asks legal question.", False, FAQActionParams),
        ("ask_privacy", "User asks privacy question.", False, FAQActionParams),
        ("end_call", "User ends the call naturally.", True, EmptyActionParams),
    ]:
        registry.register(
            RegisteredUserAction(
                name=name,
                description=desc,
                param_model=params,
                terminates_episode=terminates,
            )
        )
    return registry
