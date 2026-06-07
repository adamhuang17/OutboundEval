from __future__ import annotations

from outbound_eval.domain.enums import Severity, Verdict
from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec


class FlowChecker:
    name = "FlowChecker"
    version = "1.0"

    async def evaluate(self, task_spec: TaskSpec, scenario: ScenarioSpec, episode: EpisodeExecution) -> list[JudgeEvent]:
        assistant_text = "\n".join(turn.content for turn in episode.turns if turn.role == "assistant")
        evidence_turns = [turn for turn in episode.turns if turn.role == "assistant"]
        events: list[JudgeEvent] = []
        for req in task_spec.requirements:
            if req.id not in scenario.covered_requirement_ids or str(req.category) != "flow":
                continue
            score = self._keyword_hit(req.source_text, assistant_text)
            verdict = Verdict.PASS if score >= 0.35 else Verdict.PARTIAL if score >= 0.15 else Verdict.FAIL
            events.append(
                JudgeEvent(
                    id=f"judge.flow.{stable_hash(episode.episode_id + req.id)}",
                    run_id=episode.run_id,
                    episode_id=episode.episode_id,
                    checker_name=self.name,
                    checker_version=self.version,
                    requirement_id=req.id,
                    rubric_item_id=self._rubric(task_spec, req.id),
                    verdict=verdict,
                    confidence=0.75,
                    evidence_turn_ids=[turn.id for turn in evidence_turns[-2:]],
                    evidence_quotes=[turn.content for turn in evidence_turns[-2:]],
                    reason=f"Flow keyword coverage={score:.2f}.",
                    score_delta=0.0,
                    severity=req.severity,
                    raw_output={"coverage": score},
                )
            )
        return events

    def _keyword_hit(self, source: str, text: str) -> float:
        words = [part for part in source.replace("，", " ").replace("。", " ").replace(",", " ").split() if len(part) >= 2]
        if not words:
            chars = {char for char in source if "\u4e00" <= char <= "\u9fff"}
            return len(chars & set(text)) / max(1, len(chars))
        return sum(1 for word in words if word in text) / len(words)

    def _rubric(self, task_spec: TaskSpec, requirement_id: str) -> str | None:
        item = next((rubric for rubric in task_spec.rubric if requirement_id in rubric.linked_requirement_ids), None)
        return item.rubric_id if item else None

