from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import EpisodeStatus, TurnRole
from outbound_eval.domain.ids import timestamped_id
from outbound_eval.domain.schemas_episode import EpisodeExecution, TurnEvent
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_understanding import (
    ScenarioSpec,
    TaskUnderstanding,
    UserSimulatorOutput,
)
from outbound_eval.llm.structured_client import StructuredLLMClient
from outbound_eval.simulator.visibility_filter import VisibilityFilter


class DialogueRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: EpisodeExecution
    target_payloads: list[dict] = Field(default_factory=list)
    visibility_violations: list[str] = Field(default_factory=list)


class DialogueManager:
    """Observe-act loop for target model and user simulator."""

    def __init__(
        self,
        client: StructuredLLMClient | None = None,
        visibility_filter: VisibilityFilter | None = None,
    ):
        from outbound_eval.llm.structured_client import get_client

        self._client = client or get_client()
        self._visibility = visibility_filter or VisibilityFilter()

    async def run_episode(
        self,
        *,
        run_id: str,
        understanding: TaskUnderstanding,
        scenario: ScenarioSpec,
        raw_instruction: str,
        target_model_config: ModelConfig,
        simulator_model_config: ModelConfig,
        variables: dict | None = None,
        episode_id: str | None = None,
    ) -> DialogueRunResult:
        episode_id = episode_id or timestamped_id("ep")
        task_id = understanding.task_spec.get("task_id", "task_unknown")
        episode = EpisodeExecution(
            run_id=run_id,
            episode_id=episode_id,
            task_id=task_id,
            scenario_id=scenario.scenario_id,
            status=EpisodeStatus.RUNNING,
        )
        target_payloads: list[dict] = []
        violations: list[str] = []
        stalled_count = 0
        last_pair: tuple[str, str] | None = None

        self._append_turn(
            episode,
            run_id=run_id,
            role=TurnRole.USER,
            content=scenario.initial_user_utterance or "您好",
            related_requirement_ids=scenario.covered_requirement_ids,
            metadata={"simulator_state": "initial", "intent": "initial_utterance"},
        )

        try:
            for _ in range(max(1, min(scenario.max_turns, 12))):
                visible = self._visibility.target_context(
                    understanding=understanding,
                    scenario=scenario,
                    raw_instruction=raw_instruction,
                    variables=variables or {},
                    turns=episode.turns,
                )
                violations.extend(visible.leakage_violations)
                target_payloads.append(
                    {
                        "episode_id": episode_id,
                        "scenario_id": scenario.scenario_id,
                        "model": target_model_config.model_name,
                        "messages": visible.messages,
                        "leakage_violations": visible.leakage_violations,
                    }
                )
                if visible.leakage_violations:
                    episode.termination_reason = "visibility_leakage"
                    break

                target_reply = await self._client.invoke_text(
                    model_config=target_model_config,
                    messages=visible.messages,
                    temperature=target_model_config.temperature,
                )
                self._append_turn(
                    episode,
                    run_id=run_id,
                    role=TurnRole.ASSISTANT,
                    content=target_reply,
                    related_requirement_ids=scenario.covered_requirement_ids,
                    metadata={"model": target_model_config.model_name},
                )

                sim_output = await self._simulate_user(
                    understanding=understanding,
                    scenario=scenario,
                    episode=episode,
                    model_config=simulator_model_config,
                )
                if not sim_output.utterance.strip():
                    episode.termination_reason = sim_output.stop_reason or "simulator_empty_utterance"
                    break
                if last_pair == (target_reply.strip(), sim_output.utterance.strip()):
                    stalled_count += 1
                else:
                    stalled_count = 0
                last_pair = (target_reply.strip(), sim_output.utterance.strip())

                self._append_turn(
                    episode,
                    run_id=run_id,
                    role=TurnRole.USER,
                    content=sim_output.utterance,
                    related_requirement_ids=scenario.covered_requirement_ids,
                    metadata={
                        "simulator_state": sim_output.state,
                        "intent": sim_output.intent,
                        "memory_update": sim_output.memory_update,
                        "covered_judge_point_ids": sim_output.covered_judge_point_ids,
                    },
                )
                if not sim_output.should_continue:
                    episode.termination_reason = sim_output.stop_reason or "simulator_stop"
                    break
                if stalled_count >= 2:
                    episode.termination_reason = "loop_detected"
                    break
            else:
                episode.termination_reason = "max_turns"
            episode.status = EpisodeStatus.COMPLETED if not violations else EpisodeStatus.FAILED
            if violations:
                episode.error = {"stage": "visibility_filter", "violations": violations}
        except Exception as exc:
            episode.status = EpisodeStatus.FAILED
            episode.termination_reason = "dialogue_error"
            episode.error = {"stage": "dialogue_manager", "error_type": exc.__class__.__name__, "message": str(exc)}
        return DialogueRunResult(episode=episode, target_payloads=target_payloads, visibility_violations=violations)

    async def _simulate_user(
        self,
        *,
        understanding: TaskUnderstanding,
        scenario: ScenarioSpec,
        episode: EpisodeExecution,
        model_config: ModelConfig,
    ) -> UserSimulatorOutput:
        transcript = "\n".join(f"[{turn.role}] ({turn.id}): {turn.content}" for turn in episode.turns)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是外呼评测中的模拟用户。你可以看到隐藏用户目标和场景，但绝不能把测试目的、"
                    "评分点、expected_model_behavior 或隐藏目标原样说给对话模型。\n"
                    "每次只输出一句自然电话话术，并给出状态 JSON。"
                ),
            },
            {
                "role": "user",
                "content": f"""任务摘要：{understanding.task_spec.get('objective', '')}
场景：{scenario.title}
用户表面目标：{scenario.user_goal}
隐藏用户目标：{scenario.hidden_user_goal}
画像：{scenario.persona.model_dump(mode='json')}
对话推进方向：{scenario.dialogue_direction}
停止条件：{scenario.stop_conditions}

当前对话：
{transcript}

输出 JSON:
{{
  "utterance": "下一句自然用户话术",
  "intent": "本轮意图",
  "state": "active|satisfied|blocked|hangup",
  "memory_update": "短记忆更新",
  "should_continue": true,
  "covered_judge_point_ids": [],
  "stop_reason": null
}}""",
            },
        ]
        result = await self._client.invoke_json(
            model_config=model_config,
            messages=messages,
            output_model=UserSimulatorOutput,
            stage="dialogue_simulator_turn",
            temperature=max(model_config.temperature, 0.5),
        )
        return result.parsed

    def _append_turn(
        self,
        episode: EpisodeExecution,
        *,
        run_id: str,
        role: TurnRole,
        content: str,
        related_requirement_ids: list[str] | None = None,
        metadata: dict | None = None,
    ) -> TurnEvent:
        turn = TurnEvent(
            id=timestamped_id("turn"),
            run_id=run_id,
            episode_id=episode.episode_id,
            turn_index=len(episode.turns),
            role=role,
            content=content,
            related_requirement_ids=related_requirement_ids or [],
            metadata=metadata or {},
        )
        episode.turns.append(turn)
        return turn
