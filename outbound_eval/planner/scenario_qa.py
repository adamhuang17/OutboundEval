from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import FindingDecision, FindingSource, Severity
from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_judge import SpecFinding
from outbound_eval.domain.schemas_understanding import ScenarioSet, TaskUnderstanding


class ScenarioQAResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    passed: bool
    findings: list[SpecFinding] = Field(default_factory=list)
    blocking_findings: list[SpecFinding] = Field(default_factory=list)
    judge_point_coverage: dict[str, list[str]] = Field(default_factory=dict)
    requirement_coverage: dict[str, list[str]] = Field(default_factory=dict)
    risk_coverage: dict[str, list[str]] = Field(default_factory=dict)


class ScenarioQAGate:
    """Validate ScenarioSet after LLM planning/building."""

    def validate(self, understanding: TaskUnderstanding, scenario_set: ScenarioSet) -> ScenarioQAResult:
        task_spec = understanding.task_spec or {}
        req_ids = {str(req.get("id", "")) for req in task_spec.get("requirements", []) if req.get("id")}
        jp_ids = {jp.id for jp in understanding.judge_plan.judge_points}
        findings: list[SpecFinding] = []
        jp_coverage: dict[str, list[str]] = {}
        req_coverage: dict[str, list[str]] = {}
        risk_coverage: dict[str, list[str]] = {}

        if not scenario_set.scenarios:
            findings.append(self._finding("SCENARIOS_EMPTY", "ScenarioSet.scenarios is empty.", "scenarios", Severity.CRITICAL, True))

        scenario_ids = [scenario.scenario_id for scenario in scenario_set.scenarios]
        duplicates = [sid for sid, count in Counter(scenario_ids).items() if count > 1]
        for sid in duplicates:
            findings.append(self._finding("SCENARIO_ID_DUPLICATE", f"Duplicate scenario_id: {sid}.", sid, Severity.MAJOR, True))

        for scenario in scenario_set.scenarios:
            if not scenario.initial_user_utterance.strip():
                findings.append(
                    self._finding("INITIAL_UTTERANCE_MISSING", f"{scenario.scenario_id} has no initial_user_utterance.", scenario.scenario_id, Severity.MAJOR, True)
                )
            if not scenario.hidden_user_goal.strip():
                findings.append(
                    self._finding("HIDDEN_GOAL_MISSING", f"{scenario.scenario_id} has no hidden_user_goal.", scenario.scenario_id, Severity.MAJOR, True)
                )
            if not scenario.linked_judge_point_ids:
                findings.append(
                    self._finding("SCENARIO_UNLINKED_JUDGE_POINT", f"{scenario.scenario_id} links no judge points.", scenario.scenario_id, Severity.MAJOR, True)
                )
            for jp_id in scenario.linked_judge_point_ids:
                if jp_id not in jp_ids:
                    findings.append(
                        self._finding("SCENARIO_UNKNOWN_JUDGE_POINT", f"{scenario.scenario_id} links unknown judge point {jp_id}.", scenario.scenario_id, Severity.MAJOR, True)
                    )
                else:
                    jp_coverage.setdefault(jp_id, []).append(scenario.scenario_id)
            for req_id in scenario.covered_requirement_ids:
                if req_id not in req_ids:
                    findings.append(
                        self._finding("SCENARIO_UNKNOWN_REQUIREMENT", f"{scenario.scenario_id} covers unknown requirement {req_id}.", scenario.scenario_id, Severity.MAJOR, True)
                    )
                else:
                    req_coverage.setdefault(req_id, []).append(scenario.scenario_id)
            for risk_id in scenario.metadata.get("risk_coverage_requirement_ids", []) or []:
                risk_coverage.setdefault(str(risk_id), []).append(scenario.scenario_id)

        for point in understanding.judge_plan.judge_points:
            if point.id not in jp_coverage:
                findings.append(
                    self._finding(
                        "JUDGE_POINT_UNCOVERED",
                        f"JudgePoint {point.id} is not covered by any scenario.",
                        point.id,
                        point.severity,
                        True,
                    )
                )

        for risk_req in understanding.risk_plan.coverage_requirements:
            count = len(risk_coverage.get(risk_req.id, []))
            if count < risk_req.min_scenarios:
                findings.append(
                    self._finding(
                        "RISK_COVERAGE_UNDERFILLED",
                        f"Risk coverage {risk_req.id} requires {risk_req.min_scenarios} scenarios but only has {count}.",
                        risk_req.id,
                        risk_req.priority,
                        True,
                    )
                )

        blocking = [finding for finding in findings if finding.blocking]
        return ScenarioQAResult(
            passed=not blocking,
            findings=findings,
            blocking_findings=blocking,
            judge_point_coverage=jp_coverage,
            requirement_coverage=req_coverage,
            risk_coverage=risk_coverage,
        )

    def _finding(self, code: str, detail: str, ref: str, severity: Severity | str, blocking: bool) -> SpecFinding:
        return SpecFinding(
            id=f"scenario.{code.lower()}.{stable_hash(code + ref + detail)[:8]}",
            source=FindingSource.COMPLETENESS,
            severity=severity,
            requirement_ref=ref,
            detail=detail,
            suggested_fix="Regenerate or repair scenarios before starting a run.",
            decision=FindingDecision.HUMAN_NEEDED,
            blocking=blocking,
            metadata={"code": code, "stage": "scenario_qa"},
        )
