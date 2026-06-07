from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.adapters.openai_compatible import OpenAICompatibleAdapter
from outbound_eval.domain.enums import EpisodeStatus, TurnRole
from outbound_eval.domain.ids import timestamped_id, turn_id
from outbound_eval.domain.schemas_episode import EpisodeExecution, ModelCallError, ModelCallEvent, ModelTurn, TurnEvent
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_score import ScoreSummary
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.evaluator.ensemble import EvaluatorEnsemble
from outbound_eval.scoring.aggregator import ScoreAggregator
from outbound_eval.simulator.user_simulator import LLMUserSimulator, SimulatorMemory
from outbound_eval.trace.store import SQLiteTraceStore


class EpisodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: EpisodeExecution
    judges: list[JudgeEvent] = Field(default_factory=list)
    score: ScoreSummary | None = None


class EvaluationRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    episode_results: list[EpisodeResult]
    status: str = "completed"


class EpisodeRunner:
    def __init__(
        self,
        adapter: Any | None = None,
        simulator: LLMUserSimulator | None = None,
        evaluator: EvaluatorEnsemble | None = None,
        scorer: ScoreAggregator | None = None,
        trace_store: SQLiteTraceStore | None = None,
    ):
        self.adapter = adapter or OpenAICompatibleAdapter()
        self.simulator = simulator or LLMUserSimulator()
        self.evaluator = evaluator or EvaluatorEnsemble()
        self.scorer = scorer or ScoreAggregator()
        self.trace_store = trace_store
        self.simulator_model_config: ModelConfig | None = None
        self.audit_payload_dir: Path | None = None

    async def run_episode(
        self,
        task_spec: TaskSpec,
        scenario: ScenarioSpec,
        model_config: ModelConfig,
        run_id: str | None = None,
        attempt: int = 1,
    ) -> EpisodeResult:
        if not model_config.connection_tested:
            raise ValueError("model_config.connection_tested must be true before run_episode")
        run_id = run_id or timestamped_id("run")
        episode_id = f"ep_{scenario.scenario_id}_attempt_{attempt}"
        episode = EpisodeExecution(
            run_id=run_id,
            episode_id=episode_id,
            task_id=task_spec.task_id,
            scenario_id=scenario.scenario_id,
            attempt=attempt,
            status=EpisodeStatus.RUNNING,
        )
        if self.trace_store:
            self.trace_store.write_episode(episode)
        memory = SimulatorMemory()
        model_turn: ModelTurn | None = None
        session = await self.adapter.start_session(task_spec, {}, model_config)
        try:
            for _ in range(scenario.max_turns):
                user_turn, sim_state, user_done = await self.simulator.observe_and_respond_async(
                    run_id, episode_id, scenario, model_turn, memory, self.simulator_model_config
                )
                episode.turns.append(user_turn)
                episode.simulator_events.append(sim_state)
                if self.trace_store:
                    self.trace_store.write_turn(user_turn)
                if user_done:
                    episode.termination_reason = "simulator_stop"
                    break
                messages = self.simulator.target_visible_context(task_spec.source_text or task_spec.objective, {}, episode.turns)
                self._write_target_payload(
                    {
                        "run_id": run_id,
                        "episode_id": episode_id,
                        "scenario_id": scenario.scenario_id,
                        "model": model_config.model_name,
                        "base_url": model_config.base_url,
                        "messages": messages,
                    }
                )
                try:
                    model_turn = await self.adapter.send_turn(
                        session,
                        messages,
                        metadata={"model_config": self._raw_model_config(model_config)},
                    )
                    model_event = self._model_call_event(run_id, episode_id, model_config, model_turn)
                    episode.model_calls.append(model_event)
                    assistant_turn = TurnEvent(
                        id=turn_id(episode_id, len(episode.turns) + 1),
                        run_id=run_id,
                        episode_id=episode_id,
                        turn_index=len(episode.turns) + 1,
                        role=TurnRole.ASSISTANT,
                        content=model_turn.content,
                        related_requirement_ids=scenario.covered_requirement_ids,
                        metadata={"finish_reason": model_turn.finish_reason},
                    )
                    episode.turns.append(assistant_turn)
                    if self.trace_store:
                        self.trace_store.write_model_call(model_event)
                        self.trace_store.write_turn(assistant_turn)
                except Exception as exc:
                    error_turn = TurnEvent(
                        id=turn_id(episode_id, len(episode.turns) + 1),
                        run_id=run_id,
                        episode_id=episode_id,
                        turn_index=len(episode.turns) + 1,
                        role=TurnRole.SYSTEM,
                        content=f"system_error: {exc}",
                        related_requirement_ids=scenario.covered_requirement_ids,
                        metadata={"error_type": exc.__class__.__name__},
                    )
                    episode.turns.append(error_turn)
                    episode.model_calls.append(
                        ModelCallEvent(
                            id=f"modelcall.{episode_id}.{len(episode.model_calls) + 1}",
                            run_id=run_id,
                            episode_id=episode_id,
                            base_url=model_config.base_url,
                            model_name=model_config.model_name,
                            error=ModelCallError(error_type=exc.__class__.__name__, error_message=str(exc), retryable=False),
                        )
                    )
                    episode.status = EpisodeStatus.FAILED
                    episode.error = {"stage": "target_model_turn", "error_type": exc.__class__.__name__, "message": str(exc)}
                    episode.termination_reason = "model_error"
                    if self.trace_store:
                        self.trace_store.write_turn(error_turn)
                    break
            else:
                episode.termination_reason = "max_turns"
            if episode.status != EpisodeStatus.FAILED:
                episode.status = EpisodeStatus.COMPLETED
            episode.finished_at = datetime.now(timezone.utc)
        finally:
            await self.adapter.close_session(session)

        judges = await self.evaluator.evaluate(task_spec, scenario, episode)
        for judge in judges:
            if self.trace_store:
                self.trace_store.write_judge(judge)
        score = self.scorer.aggregate(task_spec, judges, run_id=run_id, episode_id=episode.episode_id)
        return EpisodeResult(episode=episode, judges=judges, score=score)

    def _model_call_event(self, run_id: str, episode_id: str, config: ModelConfig, turn: ModelTurn) -> ModelCallEvent:
        raw = turn.raw_output or {}
        return ModelCallEvent(
            id=f"modelcall.{episode_id}.{len(raw) + int(datetime.now().timestamp() * 1000)}",
            run_id=run_id,
            episode_id=episode_id,
            request_id=raw.get("id"),
            base_url=config.base_url,
            model_name=config.model_name,
            latency_ms=turn.latency_ms,
            prompt_tokens=turn.prompt_tokens,
            completion_tokens=turn.completion_tokens,
            raw_response_hash=raw.get("raw_response_hash"),
        )

    def _raw_model_config(self, config: ModelConfig) -> dict:
        return {
            "provider": config.provider,
            "base_url": config.base_url,
            "api_key": config.raw_api_key(),
            "model_name": config.model_name,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "timeout_seconds": config.timeout_seconds,
            "connection_tested": config.connection_tested,
        }

    def _write_target_payload(self, payload: dict[str, Any]) -> None:
        if not self.audit_payload_dir:
            return
        self.audit_payload_dir.mkdir(parents=True, exist_ok=True)
        path = self.audit_payload_dir / "target_request_payloads.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


class BatchRunner:
    def __init__(self, episode_runner: EpisodeRunner | None = None):
        self.episode_runner = episode_runner or EpisodeRunner()

    async def run_matrix(
        self,
        task_spec: TaskSpec,
        scenarios: list[ScenarioSpec],
        model_configs: list[ModelConfig],
        attempts: int = 1,
        parallel: int = 1,
    ) -> EvaluationRunResult:
        run_id = timestamped_id("run")
        semaphore = asyncio.Semaphore(parallel)
        results: list[EpisodeResult] = []

        async def run_one(scenario: ScenarioSpec, config: ModelConfig, attempt: int) -> None:
            async with semaphore:
                results.append(await self.episode_runner.run_episode(task_spec, scenario, config, run_id=run_id, attempt=attempt))

        await asyncio.gather(
            *[
                run_one(scenario, config, attempt)
                for scenario in scenarios
                for config in model_configs
                for attempt in range(1, attempts + 1)
            ]
        )
        return EvaluationRunResult(run_id=run_id, episode_results=results)
