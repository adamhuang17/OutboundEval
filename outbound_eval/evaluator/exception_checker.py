from __future__ import annotations

from outbound_eval.domain.enums import Severity, Verdict
from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec


class ExceptionChecker:
    name = "ExceptionChecker"
    version = "1.0"

    async def evaluate(self, task_spec: TaskSpec, scenario: ScenarioSpec, episode: EpisodeExecution) -> list[JudgeEvent]:
        if str(scenario.scenario_type) != "exception":
            return []
        text = "\n".join(turn.content for turn in episode.turns if turn.role == "assistant")
        evidence = [turn for turn in episode.turns if turn.role == "assistant"][-2:]
        events: list[JudgeEvent] = []
        for req in task_spec.requirements:
            if req.id not in scenario.covered_requirement_ids or str(req.category) not in {"exception", "termination"}:
                continue
            if any(term in " ".join(scenario.user_prior_conditions) for term in ("开车", "忙")):
                ok = any(term in text for term in ("安全", "不打扰", "稍后", "结束"))
            elif "拒绝" in " ".join(scenario.user_prior_conditions):
                ok = any(term in text for term in ("理解", "记录", "不强求", "稍后"))
            else:
                ok = bool(text)
            events.append(
                JudgeEvent(
                    id=f"judge.exception.{stable_hash(episode.episode_id + req.id)}",
                    run_id=episode.run_id,
                    episode_id=episode.episode_id,
                    checker_name=self.name,
                    checker_version=self.version,
                    requirement_id=req.id,
                    rubric_item_id=self._rubric(task_spec, req.id),
                    verdict=Verdict.PASS if ok else Verdict.FAIL,
                    confidence=0.75,
                    evidence_turn_ids=[turn.id for turn in evidence],
                    evidence_quotes=[turn.content for turn in evidence],
                    reason="Exception handling checked against scenario prior conditions.",
                    score_delta=0.0,
                    severity=Severity.MAJOR,
                    raw_output=None,
                )
            )
        return events

    def _rubric(self, task_spec: TaskSpec, requirement_id: str) -> str | None:
        item = next((rubric for rubric in task_spec.rubric if requirement_id in rubric.linked_requirement_ids), None)
        return item.rubric_id if item else None

