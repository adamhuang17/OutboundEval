from __future__ import annotations

from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_report import GoldenCase, GoldenLabel


class GoldenSetService:
    def sample_cases(self, task_id: str, scenario_ids: list[str], requirement_ids: list[str]) -> tuple[list[GoldenCase], list[GoldenLabel]]:
        cases: list[GoldenCase] = []
        labels: list[GoldenLabel] = []
        for index, scenario_id in enumerate(scenario_ids[:2], start=1):
            case = GoldenCase(
                id=f"golden.case.{stable_hash(task_id + scenario_id)}",
                task_id=task_id,
                scenario_id=scenario_id,
                description=f"Seed golden case {index} for {scenario_id}",
            )
            cases.append(case)
            for req_id in requirement_ids[:2]:
                labels.append(
                    GoldenLabel(
                        id=f"golden.label.{stable_hash(case.id + req_id)}",
                        golden_case_id=case.id,
                        requirement_id=req_id,
                        expected_verdict="pass",
                        rationale="Seed label for judge calibration.",
                    )
                )
        return cases, labels

