from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import FindingDecision, FindingSource, Severity
from outbound_eval.domain.schemas_judge import SpecFinding
from outbound_eval.domain.schemas_task import DetectedRisk, RiskCoverageRequirement, RiskGuardStatus, TaskSpec
from outbound_eval.spec_qa.ambiguity_auditor import AmbiguityAuditor
from outbound_eval.spec_qa.completeness_auditor import CompletenessAuditor
from outbound_eval.spec_qa.risk_auditor import RiskAuditor
from outbound_eval.spec_qa.triage import SpecQATriage


class SpecQAResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    passed: bool
    findings: list[SpecFinding] = Field(default_factory=list)
    dismissed_count: int = 0
    blocking_findings: list[SpecFinding] = Field(default_factory=list)
    detected_risks: list[DetectedRisk] = Field(default_factory=list)
    risk_guard_statuses: list[RiskGuardStatus] = Field(default_factory=list)
    risk_coverage_requirements: list[RiskCoverageRequirement] = Field(default_factory=list)


class SpecQAService:
    def __init__(self) -> None:
        self.auditors = [CompletenessAuditor(), AmbiguityAuditor(), RiskAuditor()]
        self.triage = SpecQATriage()

    async def audit(self, raw_instruction: str, task_spec: TaskSpec) -> SpecQAResult:
        findings: list[SpecFinding] = []
        for auditor in self.auditors:
            findings.extend(await auditor.audit(raw_instruction, task_spec))
        normalized = self.triage.normalize(findings)
        deduped = self.triage.dedupe(normalized)
        classified = self.triage.classify(deduped)
        active = self.triage.active(classified)
        blocking = [finding for finding in active if self.is_blocking_finding(finding)]
        return SpecQAResult(
            passed=len(blocking) == 0,
            findings=classified,
            dismissed_count=sum(1 for item in classified if item.dismissed),
            blocking_findings=blocking,
            detected_risks=task_spec.detected_risks,
            risk_guard_statuses=task_spec.risk_guard_statuses,
            risk_coverage_requirements=task_spec.risk_coverage_requirements,
        )

    def is_blocking_finding(self, finding: SpecFinding) -> bool:
        if finding.dismissed:
            return False
        if finding.blocking is True:
            return True
        missing = finding.metadata.get("missing_guards", [])
        return (
            finding.source == FindingSource.RISK
            and finding.severity == Severity.CRITICAL
            and finding.decision == FindingDecision.HUMAN_NEEDED
            and bool(missing)
        )
