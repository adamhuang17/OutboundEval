from __future__ import annotations

from outbound_eval.domain.enums import Severity, Verdict
from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec


class KnowledgeChecker:
    name = "KnowledgeChecker"
    version = "1.0"

    async def evaluate(self, task_spec: TaskSpec, scenario: ScenarioSpec, episode: EpisodeExecution) -> list[JudgeEvent]:
        assistant_turns = [turn for turn in episode.turns if turn.role == "assistant"]
        assistant_text = "\n".join(turn.content for turn in assistant_turns)
        events: list[JudgeEvent] = []

        # Build risk-linked FAQ index: faq_id → [risk_category_id, ...]
        risk_by_faq: dict[str, list[str]] = {}
        for risk in task_spec.detected_risks:
            for faq_id in risk.matched_faq_fact_ids:
                risk_by_faq.setdefault(faq_id, []).append(risk.risk_category_id)

        for fact in task_spec.faq_facts:
            if not set(fact.requirement_ids) & set(scenario.covered_requirement_ids):
                continue
            hit_rate = self._hit_rate(fact.answer, assistant_text)
            verdict = Verdict.PASS if hit_rate >= 0.45 else Verdict.PARTIAL if hit_rate >= 0.2 else Verdict.FAIL
            req_id = fact.requirement_ids[0] if fact.requirement_ids else None
            risk_category_ids = risk_by_faq.get(fact.id, [])
            events.append(
                JudgeEvent(
                    id=f"judge.knowledge.{stable_hash(episode.episode_id + fact.id)}",
                    run_id=episode.run_id,
                    episode_id=episode.episode_id,
                    checker_name=self.name,
                    checker_version=self.version,
                    requirement_id=req_id,
                    rubric_item_id=self._rubric(task_spec, req_id),
                    verdict=verdict,
                    confidence=0.8,
                    evidence_turn_ids=[turn.id for turn in assistant_turns[-2:]],
                    evidence_quotes=[turn.content for turn in assistant_turns[-2:]],
                    reason=f"FAQ answer hit_rate={hit_rate:.2f}.",
                    score_delta=0.0,
                    severity=Severity.MAJOR,
                    raw_output={
                        "hit_rate": hit_rate,
                        "grounding_source": fact.grounding_source,
                        "risk_linked": bool(risk_category_ids),
                        "risk_category_ids": risk_category_ids,
                    },
                )
            )
        return events

    def _hit_rate(self, expected: str, actual: str) -> float:
        tokens = [token for token in expected.replace("，", " ").replace("。", " ").replace(",", " ").split() if token]
        if not tokens:
            chars = {char for char in expected if "\u4e00" <= char <= "\u9fff"}
            return len(chars & set(actual)) / max(1, len(chars))
        return sum(1 for token in tokens if token in actual) / len(tokens)

    def _rubric(self, task_spec: TaskSpec, requirement_id: str | None) -> str | None:
        if not requirement_id:
            return None
        item = next((rubric for rubric in task_spec.rubric if requirement_id in rubric.linked_requirement_ids), None)
        return item.rubric_id if item else None

