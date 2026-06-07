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
        ("ask_detail", "User asks a knowledge, policy, or procedure detail.", False, FAQActionParams),
        ("challenge_constraint", "User challenges a boundary, limitation, or unsupported request.", False, FAQActionParams),
        ("refuse", "User refuses the request.", False, EmptyActionParams),
        ("say_busy", "User says they are busy.", False, EmptyActionParams),
        ("say_unavailable", "User says they are unavailable.", False, EmptyActionParams),
        ("interrupt", "User interrupts or changes topic.", False, EmptyActionParams),
        ("claim_not_responsible", "User says they are not responsible.", False, EmptyActionParams),
        ("claim_cannot_operate", "User says they cannot perform the requested action.", False, EmptyActionParams),
        ("ask_out_of_scope", "User asks for an unsupported commitment or unrelated handling.", False, FAQActionParams),
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
