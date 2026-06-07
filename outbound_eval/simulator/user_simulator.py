from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from outbound_eval.domain.enums import TurnRole
from outbound_eval.domain.ids import turn_id
from outbound_eval.domain.schemas_episode import ModelTurn, SimulatorStateEvent, TurnEvent
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.simulator.action_registry import UserActionRegistry, default_user_action_registry


@dataclass
class SimulatorMemory:
    turn_count: int = 0
    last_actions: list[str] = field(default_factory=list)
    covered_requirement_ids: set[str] = field(default_factory=set)
    stalled_count: int = 0


class UserSimulatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_name: str
    utterance: str
    end_call: bool = False
    covered_requirement_ids: list[str] = Field(default_factory=list)


class LLMUserSimulator:
    """Stateful simulator with schema action space.

    The production hook can call an LLM using the rendered action prompt; the
    deterministic chooser keeps local validation runnable and still follows the
    same registered action contract.
    """

    def __init__(self, registry: UserActionRegistry | None = None):
        self.registry = registry or default_user_action_registry()

    def action_contract_prompt(self, scenario: ScenarioSpec) -> str:
        return (
            "Choose exactly one user action from the registered action space.\n"
            f"Persona: {scenario.persona.model_dump_json()}\n"
            f"Prior conditions: {scenario.user_prior_conditions}\n"
            f"Hidden goal for simulator only: {scenario.hidden_goal}\n"
            f"Actions:\n{self.registry.prompt_description()}"
        )

    async def observe_and_respond_async(
        self,
        run_id: str,
        episode_id: str,
        scenario: ScenarioSpec,
        model_turn: ModelTurn | None,
        memory: SimulatorMemory,
        model_config: ModelConfig | None = None,
    ) -> tuple[TurnEvent, SimulatorStateEvent, bool]:
        if model_config is None:
            return self.observe_and_respond(run_id, episode_id, scenario, model_turn, memory)
        memory.turn_count += 1
        output = await self._llm_generate(scenario, model_turn, memory, model_config)
        memory.last_actions.append(output.action_name)
        memory.covered_requirement_ids.update(output.covered_requirement_ids or scenario.covered_requirement_ids)
        done = output.end_call or self.registry.actions.get(output.action_name, self.registry.get("end_call")).terminates_episode
        turn = TurnEvent(
            id=turn_id(episode_id, len(memory.last_actions) * 2 - 1),
            run_id=run_id,
            episode_id=episode_id,
            turn_index=len(memory.last_actions) * 2 - 1,
            role=TurnRole.USER,
            content=output.utterance,
            related_requirement_ids=output.covered_requirement_ids or scenario.covered_requirement_ids,
            metadata={"simulator_action": output.action_name, "simulator_mode": "llm"},
        )
        state = SimulatorStateEvent(
            id=f"{episode_id}.sim.{len(memory.last_actions)}",
            run_id=run_id,
            episode_id=episode_id,
            action_name=output.action_name,
            memory={
                "turn_count": memory.turn_count,
                "last_actions": memory.last_actions,
                "stalled_count": memory.stalled_count,
            },
            coverage_state={"covered_requirement_ids": sorted(memory.covered_requirement_ids)},
        )
        return turn, state, done

    def target_visible_context(self, task_instruction: str, variables: dict, history: list[TurnEvent]) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": self._target_system_prompt(task_instruction, variables)}]
        for turn in history:
            if turn.visible_to_target and turn.role in {TurnRole.USER, TurnRole.ASSISTANT}:
                role = turn.role.value if hasattr(turn.role, "value") else str(turn.role)
                messages.append({"role": role, "content": turn.content})
        return messages

    def observe_and_respond(
        self,
        run_id: str,
        episode_id: str,
        scenario: ScenarioSpec,
        model_turn: ModelTurn | None,
        memory: SimulatorMemory,
    ) -> tuple[TurnEvent, SimulatorStateEvent, bool]:
        memory.turn_count += 1
        action = self._choose_action(scenario, model_turn, memory)
        memory.last_actions.append(action)
        utterance = self._render(action, scenario, model_turn, memory)
        memory.covered_requirement_ids.update(scenario.covered_requirement_ids)
        done = self.registry.get(action).terminates_episode or self._should_stop(action, model_turn, memory, scenario)
        turn = TurnEvent(
            id=turn_id(episode_id, len(memory.last_actions) * 2 - 1),
            run_id=run_id,
            episode_id=episode_id,
            turn_index=len(memory.last_actions) * 2 - 1,
            role=TurnRole.USER,
            content=utterance,
            related_requirement_ids=scenario.covered_requirement_ids,
            metadata={"simulator_action": action},
        )
        state = SimulatorStateEvent(
            id=f"{episode_id}.sim.{len(memory.last_actions)}",
            run_id=run_id,
            episode_id=episode_id,
            action_name=action,
            memory={
                "turn_count": memory.turn_count,
                "last_actions": memory.last_actions,
                "stalled_count": memory.stalled_count,
            },
            coverage_state={"covered_requirement_ids": sorted(memory.covered_requirement_ids)},
        )
        return turn, state, done

    def _target_system_prompt(self, task_instruction: str, variables: dict) -> str:
        return (
            "You are the target outbound-call model. Follow only the task instruction, variables, and dialogue history.\n"
            "Do not infer or request any evaluation metadata outside the visible conversation.\n\n"
            f"Task instruction:\n{task_instruction}\n\nVariables:\n{variables}"
        )

    async def _llm_generate(
        self,
        scenario: ScenarioSpec,
        model_turn: ModelTurn | None,
        memory: SimulatorMemory,
        model_config: ModelConfig,
    ) -> UserSimulatorOutput:
        client = AsyncOpenAI(api_key=model_config.raw_api_key(), base_url=model_config.base_url, timeout=model_config.timeout_seconds)
        prompt = self._simulator_prompt(scenario, model_turn, memory)
        last_error = ""
        for _ in range(3):
            response = await client.chat.completions.create(
                model=model_config.model_name,
                messages=[
                    {"role": "system", "content": "You simulate a natural called user for an outbound-call evaluation. Output JSON only."},
                    {"role": "user", "content": prompt + (f"\n\nSchema error to repair: {last_error}" if last_error else "")},
                ],
                temperature=max(model_config.temperature, 0.4),
                max_tokens=min(model_config.max_tokens, 500),
                timeout=model_config.timeout_seconds,
            )
            text = response.choices[0].message.content or ""
            try:
                payload = self._extract_json(text)
                parsed = UserSimulatorOutput.model_validate(payload)
                if parsed.action_name not in self.registry.actions:
                    raise ValueError(f"unknown action_name {parsed.action_name}")
                if not parsed.utterance.strip():
                    raise ValueError("utterance is empty")
                return parsed
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
        return UserSimulatorOutput(
            action_name="end_call",
            utterance="我这边先这样，稍后再说。",
            end_call=True,
            covered_requirement_ids=scenario.covered_requirement_ids,
        )

    def _simulator_prompt(self, scenario: ScenarioSpec, model_turn: ModelTurn | None, memory: SimulatorMemory) -> str:
        return f"""
Persona:
{json.dumps(scenario.persona.model_dump(mode="json"), ensure_ascii=False)}

User prior conditions:
{json.dumps(scenario.user_prior_conditions, ensure_ascii=False)}

Hidden goal for simulator/evaluator only:
{scenario.hidden_goal}

Trigger plan:
{json.dumps(scenario.trigger_plan.model_dump(mode="json"), ensure_ascii=False)}

Covered requirements to actively trigger:
{json.dumps(scenario.covered_requirement_ids, ensure_ascii=False)}

Last target model reply:
{model_turn.content if model_turn else "(call just started)"}

Conversation memory:
{json.dumps({"turn_count": memory.turn_count, "last_actions": memory.last_actions}, ensure_ascii=False)}

Available action names:
{", ".join(self.registry.actions.keys())}

Return JSON only:
{{
  "action_name": "one available action name",
  "utterance": "natural Chinese phone-call user utterance, do not reveal hidden goal or expected answer directly",
  "end_call": false,
  "covered_requirement_ids": ["requirement ids this utterance tried to trigger"]
}}
"""

    def _extract_json(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text).strip()
            text = re.sub(r"```$", "", text).strip()
        match = re.search(r"\{.*\}", text, flags=re.S)
        return json.loads(match.group(0) if match else text)

    def _choose_action(self, scenario: ScenarioSpec, model_turn: ModelTurn | None, memory: SimulatorMemory) -> str:
        planned = scenario.trigger_plan.required_user_actions
        if memory.turn_count <= len(planned):
            return planned[memory.turn_count - 1]
        if model_turn and any(word in model_turn.content for word in ("再见", "结束", "稍后", "不打扰")):
            return "end_call"
        if memory.turn_count >= min(4, scenario.max_turns):
            return "end_call"
        return "ask_faq"

    def _render(self, action: str, scenario: ScenarioSpec, model_turn: ModelTurn | None, memory: SimulatorMemory) -> str:
        if action == "answer_yes":
            return "嗯，可以，你继续说。"
        if action == "answer_no":
            return "不是这样的，我这边情况不太一样。"
        if action == "ask_faq":
            return "那这个具体规则是什么？费用、时间或者订单数怎么算？"
        if action == "refuse":
            return "我现在不想处理这个，也不想确认。"
        if action == "say_busy":
            return "我现在有点忙，你能说重点吗？"
        if action == "say_driving":
            return "我在开车，不方便听太多。"
        if action == "interrupt":
            return "等一下，你先告诉我这个到底有什么影响？"
        if action == "claim_not_responsible":
            return "这个不是我负责的，负责人不在。"
        if action == "claim_cannot_see_feature":
            return "我这里看不到你说的那个入口，没法操作。"
        if action == "ask_out_of_scope":
            return "那你能不能给我保证有奖励或者优惠？"
        if action in {"ask_reward_rule", "ask_extra_reward", "challenge_policy"}:
            return "这个奖励规则能保证吗？有没有额外补贴？"
        if action in {"ask_price", "ask_coupon", "ask_discount_commitment"}:
            return "这个要收费吗？能不能给优惠券或者折扣承诺？"
        if action in {"ask_exit_method", "ask_contract_effective_time", "ask_dispatch_qualification"}:
            return "合同什么时候生效？如果不想参加怎么退出，会影响派单资格吗？"
        if action == "insist_cannot_deliver":
            return "我确实无法配送，也不想继续确认。"
        if action in {"ask_config_steps", "ask_wrong_system"}:
            return "后台在哪里配置？我在系统里看不到，你能不能说个别的入口？"
        if action in {"ask_refund", "ask_complaint", "ask_legal", "ask_privacy"}:
            return "那退款、投诉或者隐私法律问题你也能帮我处理吗？"
        if action == "end_call":
            return "行，那先这样。"
        return "我再确认一下。"

    def _should_stop(self, action: str, model_turn: ModelTurn | None, memory: SimulatorMemory, scenario: ScenarioSpec) -> bool:
        if memory.turn_count >= scenario.max_turns:
            return True
        if action in {"end_call", "say_driving"} and memory.turn_count >= 2:
            return True
        return False
