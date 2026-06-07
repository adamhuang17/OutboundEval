from __future__ import annotations

from typing import Protocol

from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec


class Evaluator(Protocol):
    name: str
    version: str

    async def evaluate(self, task_spec: TaskSpec, scenario: ScenarioSpec, episode: EpisodeExecution) -> list[JudgeEvent]:
        ...

