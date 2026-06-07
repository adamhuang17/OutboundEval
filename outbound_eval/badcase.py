from __future__ import annotations

from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_report import BadcaseItem
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec


class BadcaseLibrary:
    def from_judges(self, task_spec: TaskSpec, scenario: ScenarioSpec, judges: list[JudgeEvent]) -> list[BadcaseItem]:
        out: list[BadcaseItem] = []
        for judge in judges:
            if str(judge.verdict) not in {"fail", "partial"}:
                continue
            out.append(
                BadcaseItem(
                    id=f"badcase.{stable_hash(judge.id)}",
                    run_id=judge.run_id,
                    episode_id=judge.episode_id,
                    task_id=task_spec.task_id,
                    scenario_id=scenario.scenario_id,
                    failure_type=str(judge.verdict),
                    severity=str(judge.severity),
                    requirement_ids=[judge.requirement_id] if judge.requirement_id else [],
                    evidence_turn_ids=judge.evidence_turn_ids,
                    summary=judge.reason,
                    replay_config={
                        "task_id": task_spec.task_id,
                        "scenario_id": scenario.scenario_id,
                        "episode_id": judge.episode_id,
                        "rejudge_only": True,
                    },
                )
            )
        return out

