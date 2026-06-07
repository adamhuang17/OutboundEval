from __future__ import annotations

from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_score import ScoreSummary
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.evaluator.ensemble import EvaluatorEnsemble
from outbound_eval.scoring.aggregator import ScoreAggregator


class RejudgeService:
    def __init__(self, evaluator: EvaluatorEnsemble | None = None, scorer: ScoreAggregator | None = None):
        self.evaluator = evaluator or EvaluatorEnsemble()
        self.scorer = scorer or ScoreAggregator()

    async def rejudge(self, task_spec: TaskSpec, scenario: ScenarioSpec, episode: EpisodeExecution) -> tuple[list[JudgeEvent], ScoreSummary]:
        judges = await self.evaluator.evaluate(task_spec, scenario, episode)
        score = self.scorer.aggregate(task_spec, judges, run_id=episode.run_id, episode_id=episode.episode_id)
        return judges, score

