from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import FindingDecision, FindingSource, Severity
from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_judge import SpecFinding
from outbound_eval.domain.schemas_understanding import TaskUnderstanding


class CompileQAResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    passed: bool
    findings: list[SpecFinding] = Field(default_factory=list)
    blocking_findings: list[SpecFinding] = Field(default_factory=list)


class CompileQAGate:
    """Validate TaskUnderstanding before scenarios or runs can be built."""

    def validate(self, understanding: TaskUnderstanding) -> CompileQAResult:
        task_spec = understanding.task_spec or {}
        requirements = list(task_spec.get("requirements") or [])
        req_ids = {str(req.get("id", "")) for req in requirements if req.get("id")}
        source_map = understanding.source_map or {}
        findings: list[SpecFinding] = []

        if not str(task_spec.get("task_id", "")).strip():
            findings.append(self._finding("TASK_ID_MISSING", "TaskSpec.task_id is missing.", "task_id", Severity.CRITICAL))
        if not str(task_spec.get("task_name", "")).strip():
            findings.append(self._finding("TASK_NAME_MISSING", "TaskSpec.task_name is missing.", "task_name", Severity.MAJOR))
        if not str(task_spec.get("role", "")).strip():
            findings.append(self._finding("ROLE_MISSING", "TaskSpec.role is missing.", "role", Severity.MAJOR))
        if not str(task_spec.get("objective", "")).strip():
            findings.append(self._finding("OBJECTIVE_MISSING", "TaskSpec.objective is missing.", "objective", Severity.CRITICAL))
        if not requirements:
            findings.append(self._finding("REQUIREMENTS_EMPTY", "TaskSpec.requirements is empty.", "requirements", Severity.CRITICAL))
        if not understanding.judge_plan.judge_points:
            findings.append(self._finding("JUDGE_POINTS_EMPTY", "JudgePlan.judge_points is empty.", "judge_plan.judge_points", Severity.CRITICAL))
        if not source_map:
            findings.append(self._finding("SOURCE_MAP_EMPTY", "TaskUnderstanding.source_map is empty.", "source_map", Severity.CRITICAL))

        for req in requirements:
            req_id = str(req.get("id", ""))
            source_node_id = str(req.get("source_section", ""))
            source_text = str(req.get("source_text", ""))
            if not source_text.strip():
                findings.append(self._finding("REQ_SOURCE_TEXT_MISSING", f"{req_id} has no source_text.", req_id, Severity.MAJOR))
            if not source_node_id.strip() or source_node_id not in source_map:
                findings.append(
                    self._finding("REQ_SOURCE_REF_MISSING", f"{req_id} does not map to a known source node.", req_id, Severity.MAJOR)
                )
            if req_id and req_id not in source_map:
                findings.append(self._finding("REQ_ARTIFACT_SOURCE_MISSING", f"{req_id} is missing from source_map.", req_id, Severity.MAJOR))

        for fact in understanding.knowledge_facts:
            if not fact.source_text.strip():
                findings.append(self._finding("KNOWLEDGE_SOURCE_TEXT_MISSING", f"{fact.id} has no source_text.", fact.id, Severity.MAJOR))
            if not fact.source_node_id.strip() or fact.source_node_id not in source_map:
                findings.append(
                    self._finding("KNOWLEDGE_SOURCE_REF_MISSING", f"{fact.id} does not map to a known source node.", fact.id, Severity.MAJOR)
                )
            unknown = [rid for rid in fact.requirement_ids if rid not in req_ids]
            if unknown:
                findings.append(
                    self._finding(
                        "KNOWLEDGE_UNKNOWN_REQUIREMENT",
                        f"{fact.id} links unknown requirements: {unknown}.",
                        fact.id,
                        Severity.MAJOR,
                    )
                )

        for point in understanding.judge_plan.judge_points:
            if not point.criterion.strip() or not point.pass_criteria.strip() or not point.fail_criteria.strip():
                findings.append(
                    self._finding("JUDGE_POINT_INCOMPLETE", f"{point.id} lacks criterion/pass/fail criteria.", point.id, Severity.MAJOR)
                )
            if not point.linked_requirement_ids:
                findings.append(
                    self._finding("JUDGE_POINT_UNLINKED", f"{point.id} is not linked to any requirement.", point.id, Severity.MAJOR)
                )
            unknown = [rid for rid in point.linked_requirement_ids if rid not in req_ids]
            if unknown:
                findings.append(
                    self._finding("JUDGE_POINT_UNKNOWN_REQUIREMENT", f"{point.id} links unknown requirements: {unknown}.", point.id, Severity.MAJOR)
                )
            if not point.source_node_id.strip() or point.source_node_id not in source_map:
                findings.append(
                    self._finding("JUDGE_POINT_SOURCE_REF_MISSING", f"{point.id} does not map to a known source node.", point.id, Severity.MAJOR)
                )
            if point.id not in source_map:
                findings.append(self._finding("JUDGE_POINT_ARTIFACT_SOURCE_MISSING", f"{point.id} is missing from source_map.", point.id, Severity.MAJOR))

        blocking = [finding for finding in findings if finding.blocking]
        return CompileQAResult(passed=not blocking, findings=findings, blocking_findings=blocking)

    def _finding(self, code: str, detail: str, ref: str, severity: Severity) -> SpecFinding:
        return SpecFinding(
            id=f"compile.{code.lower()}.{stable_hash(code + ref + detail)[:8]}",
            source=FindingSource.COMPLETENESS,
            severity=severity,
            requirement_ref=ref,
            detail=detail,
            suggested_fix="Regenerate or repair the TaskUnderstanding before building scenarios.",
            decision=FindingDecision.HUMAN_NEEDED,
            blocking=severity in {Severity.CRITICAL, "critical"},
            metadata={"code": code, "stage": "compile_qa"},
        )
