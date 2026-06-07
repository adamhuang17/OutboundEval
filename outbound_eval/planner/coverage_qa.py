from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import FindingDecision, FindingSource, Severity
from outbound_eval.domain.schemas_judge import SpecFinding
from outbound_eval.domain.schemas_scenario import CoverageMatrix
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.planner.risk_scenario_factory import RiskScenarioFactory


class CoverageQAResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    passed: bool
    findings: list[SpecFinding] = Field(default_factory=list)
    autofilled_scenario_ids: list[str] = Field(default_factory=list)
    uncovered_risk_coverage_requirement_ids: list[str] = Field(default_factory=list)


class CoverageQA:
    def __init__(self, factory: RiskScenarioFactory | None = None):
        self.factory = factory or RiskScenarioFactory()

    def validate(self, task_spec: TaskSpec, matrix: CoverageMatrix) -> CoverageQAResult:
        uncovered = list(matrix.uncovered_risk_coverage_requirement_ids)
        findings = [self._finding(task_spec, req_id) for req_id in uncovered]
        return CoverageQAResult(passed=not uncovered, findings=findings, uncovered_risk_coverage_requirement_ids=uncovered)

    def validate_or_autofill(self, task_spec: TaskSpec, matrix: CoverageMatrix, budget: int) -> CoverageMatrix:
        if not matrix.uncovered_risk_coverage_requirement_ids:
            return matrix
        scenarios = list(matrix.scenarios)
        covered = set(matrix.risk_requirement_coverage)
        autofilled: list[str] = []
        for req in task_spec.risk_coverage_requirements:
            if req.id in covered:
                continue
            risk_scenario = self.factory.build(task_spec, req, len(scenarios) + 1)
            if len(scenarios) < budget:
                scenarios.append(risk_scenario)
            else:
                replace_index = next((i for i, scn in enumerate(scenarios) if not scn.metadata.get("risk_scenario")), None)
                if replace_index is None:
                    continue
                scenarios[replace_index] = risk_scenario
            autofilled.append(risk_scenario.scenario_id)
        from outbound_eval.planner.coverage_planner import CoveragePlanner

        return CoveragePlanner(apply_coverage_qa=False)._coverage_matrix(task_spec, scenarios[:budget])

    def _finding(self, task_spec: TaskSpec, riskcov_id: str) -> SpecFinding:
        req = next((item for item in task_spec.risk_coverage_requirements if item.id == riskcov_id), None)
        risk_category = req.risk_category_id if req else None
        return SpecFinding(
            source=FindingSource.RISK,
            severity=Severity.CRITICAL,
            requirement_ref=riskcov_id,
            detail=f"Risk coverage requirement {riskcov_id} is not covered by any scenario.",
            suggested_fix="Generate at least one required risk scenario.",
            decision=FindingDecision.HUMAN_NEEDED,
            blocking=True,
            metadata={
                "risk_category": risk_category,
                "missing_coverage_requirement_id": riskcov_id,
            },
        )

