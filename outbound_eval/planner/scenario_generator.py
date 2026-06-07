from __future__ import annotations

from outbound_eval.domain.schemas_scenario import CoverageMatrix
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.planner.coverage_planner import CoveragePlanner


class ScenarioGenerator:
    def __init__(self, planner: CoveragePlanner | None = None):
        self.planner = planner or CoveragePlanner()

    def generate(self, task_spec: TaskSpec, budget: int = 12) -> CoverageMatrix:
        return self.planner.plan(task_spec, budget=budget)

