from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import EpisodeStatus, TurnRole


class EpisodeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class TurnEvent(EpisodeModel):
    id: str
    run_id: str
    episode_id: str
    turn_index: int
    role: TurnRole
    content: str
    related_requirement_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    visible_to_target: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SimulatorStateEvent(EpisodeModel):
    id: str
    run_id: str
    episode_id: str
    action_name: str
    memory: dict[str, Any] = Field(default_factory=dict)
    coverage_state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ModelTurn(EpisodeModel):
    content: str
    finish_reason: str | None = None
    raw_output: dict[str, Any] | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class ModelCallError(EpisodeModel):
    error_type: str
    error_message: str
    retryable: bool = False


class ModelCallEvent(EpisodeModel):
    id: str
    run_id: str
    episode_id: str
    request_id: str | None = None
    base_url: str
    model_name: str
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw_response_hash: str | None = None
    error: ModelCallError | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EpisodeExecution(EpisodeModel):
    run_id: str
    episode_id: str
    task_id: str
    scenario_id: str
    attempt: int = 1
    status: EpisodeStatus = EpisodeStatus.PENDING
    turns: list[TurnEvent] = Field(default_factory=list)
    model_calls: list[ModelCallEvent] = Field(default_factory=list)
    simulator_events: list[SimulatorStateEvent] = Field(default_factory=list)
    termination_reason: str | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    def visible_history_for_target(self) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        for turn in self.turns:
            if not turn.visible_to_target:
                continue
            if turn.role in {TurnRole.USER, TurnRole.ASSISTANT, TurnRole.SYSTEM}:
                role = turn.role.value if hasattr(turn.role, "value") else str(turn.role)
                history.append({"role": role, "content": turn.content})
        return history
