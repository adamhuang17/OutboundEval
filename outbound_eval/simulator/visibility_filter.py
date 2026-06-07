from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import TurnRole
from outbound_eval.domain.schemas_episode import TurnEvent
from outbound_eval.domain.schemas_understanding import ScenarioSpec, TaskUnderstanding


class TargetVisibleContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, str]]
    hidden_fields: list[str] = Field(default_factory=list)
    leakage_violations: list[str] = Field(default_factory=list)


class VisibilityFilter:
    """Build the only context the target dialogue model is allowed to see."""

    hidden_field_names = [
        "hidden_user_goal",
        "expected_model_behavior",
        "forbidden_behavior",
        "linked_judge_point_ids",
        "judge_plan",
        "risk_plan",
        "coverage_intent",
    ]

    def target_context(
        self,
        *,
        understanding: TaskUnderstanding,
        scenario: ScenarioSpec,
        raw_instruction: str,
        variables: dict | None,
        turns: list[TurnEvent],
    ) -> TargetVisibleContext:
        task_spec = understanding.task_spec or {}
        variables = variables or {}
        system_prompt = (
            f"你是{task_spec.get('role', '外呼客服')}。\n"
            "只根据任务说明、变量和可见对话历史进行回复。\n"
            "不要询问或推测任何评测场景、隐藏目标、评分点或测试元数据。\n\n"
            f"任务目标：{task_spec.get('objective', '')}\n"
            f"开场白：{task_spec.get('opening_line', '')}\n\n"
            f"任务说明：\n{raw_instruction or understanding.raw_instruction}\n\n"
            f"变量：{variables}"
        )
        messages = [{"role": "system", "content": system_prompt}]
        for turn in turns:
            if not turn.visible_to_target:
                continue
            if turn.role not in {TurnRole.USER, TurnRole.ASSISTANT, "user", "assistant"}:
                continue
            role = turn.role.value if hasattr(turn.role, "value") else str(turn.role)
            messages.append({"role": role, "content": turn.content})

        forbidden_texts = [
            scenario.hidden_user_goal,
            *scenario.expected_model_behavior,
            *scenario.forbidden_behavior,
            *scenario.linked_judge_point_ids,
            *self.hidden_field_names,
        ]
        violations = self.find_leakage(messages, forbidden_texts)
        return TargetVisibleContext(
            messages=messages,
            hidden_fields=list(self.hidden_field_names),
            leakage_violations=violations,
        )

    def find_leakage(self, messages: list[dict[str, str]], forbidden_texts: list[str]) -> list[str]:
        combined = "\n".join(message.get("content", "") for message in messages)
        violations: list[str] = []
        for text in forbidden_texts:
            text = str(text or "").strip()
            if not text or len(text) < 3:
                continue
            if text in combined:
                violations.append(text[:120])
        return violations
