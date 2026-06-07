from __future__ import annotations

import re

from outbound_eval.domain.enums import FindingDecision, FindingSource, Severity
from outbound_eval.domain.schemas_judge import SpecFinding
from outbound_eval.domain.schemas_task import TaskSpec


class AmbiguityAuditor:
    async def audit(self, raw_instruction: str, task_spec: TaskSpec) -> list[SpecFinding]:
        findings: list[SpecFinding] = []
        req_ids = {req.id for req in task_spec.requirements}
        rubric_links = {rid for item in task_spec.rubric for rid in item.linked_requirement_ids}
        for req in task_spec.requirements:
            if req.id not in rubric_links:
                findings.append(
                    SpecFinding(
                        source=FindingSource.AMBIGUITY,
                        severity=Severity.MAJOR,
                        requirement_ref=req.id,
                        detail=f"Requirement {req.id} has no linked rubric item.",
                        suggested_fix="Generate a RubricItem linked to this requirement.",
                        decision=FindingDecision.AUTO_FIX,
                    )
                )
        for item in task_spec.rubric:
            missing = [rid for rid in item.linked_requirement_ids if rid not in req_ids]
            if missing:
                findings.append(
                    SpecFinding(
                        source=FindingSource.AMBIGUITY,
                        severity=Severity.CRITICAL,
                        requirement_ref=item.rubric_id,
                        detail=f"Rubric item links missing requirements: {missing}.",
                        suggested_fix="Drop or rewrite unknown requirement links.",
                        decision=FindingDecision.AUTO_FIX,
                    )
                )
        referenced_variables = set(re.findall(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}", raw_instruction))
        defined_variables = {variable.name for variable in task_spec.variables}
        for variable in referenced_variables - defined_variables:
            findings.append(
                SpecFinding(
                    source=FindingSource.AMBIGUITY,
                    severity=Severity.MAJOR,
                    requirement_ref=variable,
                    detail=f"Variable {variable} appears in source but is missing from TaskSpec.variables.",
                    suggested_fix="Add variable to TaskSpec.variables with source_text.",
                    decision=FindingDecision.AUTO_FIX,
                )
            )
        node_ids = {node.id for node in task_spec.flow_nodes}
        for edge in task_spec.flow_edges:
            if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids:
                findings.append(
                    SpecFinding(
                        source=FindingSource.AMBIGUITY,
                        severity=Severity.CRITICAL,
                        requirement_ref=edge.id,
                        detail="Flow edge references a missing flow node.",
                        suggested_fix="Rebuild flow graph edges after node normalization.",
                        decision=FindingDecision.AUTO_FIX,
                    )
                )
        return findings

