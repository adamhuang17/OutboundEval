from __future__ import annotations

from outbound_eval.domain.enums import Severity, Verdict
from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_score import ScoreItem, ScoreSummary, SeverityCap
from outbound_eval.domain.schemas_task import RubricItem, TaskSpec


VERDICT_SCORE = {
    Verdict.PASS: 1.0,
    Verdict.PARTIAL: 0.5,
    Verdict.FAIL: 0.0,
}


class SeverityGuard:
    def caps(self, judges: list[JudgeEvent], task_spec: TaskSpec | None = None) -> list[SeverityCap]:
        caps: list[SeverityCap] = []
        for judge in judges:
            if judge.verdict != Verdict.FAIL:
                continue
            raw = judge.raw_output or {}
            raw_cap = raw.get("cap_score")
            risk_category_id = raw.get("risk_category_id")
            source_cap_id = None
            if task_spec and risk_category_id:
                source_cap = next((cap for cap in task_spec.severity_caps if cap.risk_category_id == risk_category_id), None)
                if source_cap:
                    raw_cap = raw_cap if raw_cap is not None else source_cap.cap_score
                    source_cap_id = source_cap.id
            if judge.severity == Severity.CRITICAL:
                cap = float(raw_cap or 60.0)
            elif judge.severity == Severity.MAJOR:
                cap = float(raw_cap or 80.0)
            else:
                continue
            caps.append(
                SeverityCap(
                    reason=judge.reason,
                    cap_score=cap,
                    severity=judge.severity,
                    judge_event_id=judge.id,
                    risk_category_id=risk_category_id,
                    source_cap_id=source_cap_id,
                    forbidden_behavior_id=raw.get("forbidden_behavior_id"),
                )
            )
        return caps


class ScoreAggregator:
    def __init__(self):
        self.guard = SeverityGuard()

    def aggregate(self, task_spec: TaskSpec, judges: list[JudgeEvent], run_id: str, episode_id: str | None = None) -> ScoreSummary:
        rubric_by_req: dict[str, RubricItem] = {}
        for item in task_spec.rubric:
            for req_id in item.linked_requirement_ids:
                rubric_by_req[req_id] = item
        items: list[ScoreItem] = []
        judged_by_req: dict[str, list[JudgeEvent]] = {}
        for judge in judges:
            if judge.verdict == Verdict.NOT_TESTED or not judge.requirement_id:
                continue
            judged_by_req.setdefault(judge.requirement_id, []).append(judge)
        for req_id, req_judges in judged_by_req.items():
            rubric = rubric_by_req.get(req_id)
            if not rubric:
                continue
            verdict = self._worst(req_judges)
            raw_score = VERDICT_SCORE.get(verdict, 0.0)
            items.append(
                ScoreItem(
                    id=f"score.{stable_hash((episode_id or run_id) + req_id)}",
                    run_id=run_id,
                    episode_id=episode_id or "",
                    rubric_item_id=rubric.rubric_id,
                    requirement_id=req_id,
                    weight=rubric.weight,
                    verdict=verdict,
                    raw_score=raw_score,
                    weighted_score=raw_score * rubric.weight,
                    evidence_judge_ids=[judge.id for judge in req_judges],
                )
            )
        possible = sum(item.weight for item in items)
        total = sum(item.weighted_score for item in items)
        normalized = (total / possible * 100.0) if possible else 0.0
        caps = self.guard.caps(judges, task_spec)
        if caps:
            normalized = min(normalized, min(cap.cap_score for cap in caps))
        per_dimension: dict[str, float] = {}
        for item in items:
            rubric = next((rub for rub in task_spec.rubric if rub.rubric_id == item.rubric_item_id), None)
            if rubric:
                per_dimension.setdefault(rubric.dimension, 0.0)
                per_dimension[rubric.dimension] += item.weighted_score
        return ScoreSummary(
            run_id=run_id,
            episode_id=episode_id,
            total_score=total,
            possible_score=possible,
            normalized_score=round(normalized, 2),
            caps_applied=caps,
            per_dimension=per_dimension,
            items=items,
        )

    def _worst(self, judges: list[JudgeEvent]) -> Verdict:
        order = {Verdict.FAIL: 0, Verdict.PARTIAL: 1, Verdict.PASS: 2, Verdict.NOT_APPLICABLE: 3}
        verdicts = [judge.verdict for judge in judges if judge.verdict in order]
        return min(verdicts, key=lambda verdict: order[verdict]) if verdicts else Verdict.NOT_APPLICABLE
