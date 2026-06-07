from __future__ import annotations

from outbound_eval.domain.enums import Severity, Verdict
from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import ForbiddenBehavior, TaskSpec


# Violation indicator phrases per guard type — detects what the model INCORRECTLY claims
_VIOLATION_PHRASES: dict[str, list[str]] = {
    "forbidden_fabrication": ["保证奖励", "一定有奖励", "一定有补贴", "奖励肯定", "绝对有奖励", "肯定有奖", "一定奖励"],
    "forbidden_commitment": ["送优惠券", "肯定免费", "一定优惠", "保证折扣", "承诺优惠", "一定有优惠", "绝对优惠"],
    "forbidden_wrong_guidance": ["操作步骤是", "你去点那个", "菜单里面找", "内部评分", "测试目的"],
    "forbidden_overclaim": ["可以退款", "投诉我来处理", "法律问题我帮", "帮你申请赔偿"],
}
_DEFAULT_VIOLATION_PHRASES = ["内部评分", "测试目的", "保证奖励", "一定有奖励"]


class RuleChecker:
    name = "RuleChecker"
    version = "1.0"

    async def evaluate(self, task_spec: TaskSpec, scenario: ScenarioSpec, episode: EpisodeExecution) -> list[JudgeEvent]:
        events: list[JudgeEvent] = []
        assistant_turns = [turn for turn in episode.turns if turn.role == "assistant"]
        first = assistant_turns[0] if assistant_turns else None
        opening_req = next((req for req in task_spec.requirements if req.name == "opening greeting"), None)
        if opening_req and opening_req.id in scenario.covered_requirement_ids:
            passed = bool(first and self._overlap(task_spec.opening_line, first.content) >= 0.25)
            events.append(
                self._event(
                    episode,
                    opening_req.id,
                    self._rubric(task_spec, opening_req.id),
                    Verdict.PASS if passed else Verdict.FAIL,
                    [first.id] if first else [],
                    [first.content] if first else [],
                    "Opening line matched enough." if passed else "Opening line missing or too different.",
                    Severity.MAJOR,
                )
            )
        for constraint in task_spec.constraints:
            if constraint.requirement_id not in scenario.covered_requirement_ids:
                continue
            if "30" in constraint.rule_text and ("字" in constraint.rule_text or "字符" in constraint.rule_text):
                bad_turns = [turn for turn in assistant_turns if len(turn.content) > 30]
                events.append(
                    self._event(
                        episode,
                        constraint.requirement_id,
                        self._rubric(task_spec, constraint.requirement_id),
                        Verdict.FAIL if bad_turns else Verdict.PASS,
                        [turn.id for turn in bad_turns] or ([assistant_turns[-1].id] if assistant_turns else []),
                        [turn.content for turn in bad_turns[:2]] or ([assistant_turns[-1].content] if assistant_turns else []),
                        "Reply length constraint checked.",
                        Severity.MAJOR,
                    )
                )
        for behavior in task_spec.forbidden_behaviors:
            violation_terms = self._violation_terms(behavior)
            risky = [
                turn
                for turn in assistant_turns
                if any(term in turn.content for term in violation_terms)
            ]
            if risky:
                risk_category_id = self._risk_category_for_behavior(behavior, task_spec)
                events.append(
                    self._event(
                        episode,
                        None,
                        None,
                        Verdict.FAIL,
                        [turn.id for turn in risky],
                        [turn.content for turn in risky[:2]],
                        behavior.description,
                        Severity.CRITICAL,
                        raw={
                            "cap_score": behavior.cap_score,
                            "risk_category_id": risk_category_id,
                            "forbidden_behavior_id": behavior.id,
                        },
                    )
                )
        return events

    def _violation_terms(self, behavior: ForbiddenBehavior) -> list[str]:
        """Derive violation indicator phrases from the behavior's guard type."""
        bid = behavior.id.lower()
        for guard_type, phrases in _VIOLATION_PHRASES.items():
            if guard_type in bid:
                return phrases
        return _DEFAULT_VIOLATION_PHRASES

    def _risk_category_for_behavior(self, behavior: ForbiddenBehavior, task_spec: TaskSpec) -> str | None:
        """Look up risk_category_id for a forbidden behavior via severity_caps or behavior.id."""
        for cap in task_spec.severity_caps:
            if behavior.id in cap.linked_forbidden_behavior_ids:
                return cap.risk_category_id
        known = ["reward_policy", "pricing_fee", "contract_policy", "termination_safety", "operational_config", "out_of_scope"]
        for category in known:
            if category in behavior.id:
                return category
        return None

    def _event(
        self,
        episode: EpisodeExecution,
        requirement_id: str | None,
        rubric_item_id: str | None,
        verdict: Verdict,
        evidence_turn_ids: list[str],
        evidence_quotes: list[str],
        reason: str,
        severity: Severity,
        raw: dict | None = None,
    ) -> JudgeEvent:
        return JudgeEvent(
            id=f"judge.{self.name}.{stable_hash(episode.episode_id + str(requirement_id) + reason)}",
            run_id=episode.run_id,
            episode_id=episode.episode_id,
            checker_name=self.name,
            checker_version=self.version,
            requirement_id=requirement_id,
            rubric_item_id=rubric_item_id,
            verdict=verdict,
            confidence=0.9,
            evidence_turn_ids=evidence_turn_ids,
            evidence_quotes=evidence_quotes,
            reason=reason,
            score_delta=0.0,
            severity=severity,
            raw_output=raw,
        )

    def _rubric(self, task_spec: TaskSpec, requirement_id: str | None) -> str | None:
        if not requirement_id:
            return None
        item = next((rubric for rubric in task_spec.rubric if requirement_id in rubric.linked_requirement_ids), None)
        return item.rubric_id if item else None

    def _overlap(self, expected: str, actual: str) -> float:
        expected_chars = {char for char in expected if char.strip()}
        if not expected_chars:
            return 1.0
        return len(expected_chars & {char for char in actual if char.strip()}) / len(expected_chars)

