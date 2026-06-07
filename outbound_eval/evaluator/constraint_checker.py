from __future__ import annotations

from outbound_eval.domain.enums import Severity, Verdict
from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec


class ConstraintChecker:
    name = "ConstraintChecker"
    version = "1.0"

    async def evaluate(self, task_spec: TaskSpec, scenario: ScenarioSpec, episode: EpisodeExecution) -> list[JudgeEvent]:
        assistant_turns = [turn for turn in episode.turns if turn.role == "assistant"]
        events: list[JudgeEvent] = []
        for req in task_spec.requirements:
            if req.id not in scenario.covered_requirement_ids or str(req.category) != "constraint":
                continue
            forbidden = self._forbidden_terms(req.source_text)
            bad = [turn for turn in assistant_turns if any(term in turn.content for term in forbidden)]
            evidence = bad or assistant_turns[-1:]
            events.append(
                JudgeEvent(
                    id=f"judge.constraint.{stable_hash(episode.episode_id + req.id)}",
                    run_id=episode.run_id,
                    episode_id=episode.episode_id,
                    checker_name=self.name,
                    checker_version=self.version,
                    requirement_id=req.id,
                    rubric_item_id=self._rubric(task_spec, req.id),
                    verdict=Verdict.FAIL if bad else Verdict.PASS,
                    confidence=0.75,
                    evidence_turn_ids=[turn.id for turn in evidence],
                    evidence_quotes=[turn.content for turn in evidence],
                    reason="Constraint violation detected." if bad else "No direct constraint violation detected.",
                    score_delta=0.0,
                    severity=req.severity,
                    raw_output={"forbidden_terms": forbidden},
                )
            )
        return events

    def _forbidden_terms(self, text: str) -> list[str]:
        terms = []
        if "重复" in text:
            terms.append("刚才说过")
        if "禁止" in text or "不得" in text:
            terms.extend(["保证奖励", "一定免费", "测试目的"])
        return terms

    def _rubric(self, task_spec: TaskSpec, requirement_id: str) -> str | None:
        item = next((rubric for rubric in task_spec.rubric if requirement_id in rubric.linked_requirement_ids), None)
        return item.rubric_id if item else None

