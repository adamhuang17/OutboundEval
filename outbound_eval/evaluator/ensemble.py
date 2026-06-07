from __future__ import annotations

from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.evaluator.constraint_checker import ConstraintChecker
from outbound_eval.evaluator.exception_checker import ExceptionChecker
from outbound_eval.evaluator.flow_checker import FlowChecker
from outbound_eval.evaluator.knowledge_checker import KnowledgeChecker
from outbound_eval.evaluator.rule_checker import RuleChecker
from outbound_eval.evaluator.semantic_judge import SemanticJudge


class JudgeEnsembleResolver:
    def resolve(self, events: list[JudgeEvent]) -> list[JudgeEvent]:
        best: dict[tuple[str | None, str, str], JudgeEvent] = {}
        for event in events:
            key = (event.requirement_id, event.checker_name, event.verdict)
            if key not in best or event.confidence > best[key].confidence:
                best[key] = event
        return list(best.values())


class EvaluatorEnsemble:
    def __init__(self):
        self.evaluators = [
            RuleChecker(),
            FlowChecker(),
            KnowledgeChecker(),
            ConstraintChecker(),
            ExceptionChecker(),
            SemanticJudge(),
        ]
        self.resolver = JudgeEnsembleResolver()

    async def evaluate(self, task_spec: TaskSpec, scenario: ScenarioSpec, episode: EpisodeExecution) -> list[JudgeEvent]:
        events: list[JudgeEvent] = []
        for evaluator in self.evaluators:
            events.extend(await evaluator.evaluate(task_spec, scenario, episode))
        return self.resolver.resolve(events)

