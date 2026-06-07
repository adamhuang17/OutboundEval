from __future__ import annotations

import re

from outbound_eval.domain.enums import FindingDecision, FindingSource, Severity
from outbound_eval.domain.schemas_judge import SpecFinding
from outbound_eval.domain.schemas_task import TaskSpec


class CompletenessAuditor:
    async def audit(self, raw_instruction: str, task_spec: TaskSpec) -> list[SpecFinding]:
        findings: list[SpecFinding] = []
        if ("FAQ" in raw_instruction or "Knowledge" in raw_instruction or "知识" in raw_instruction) and not task_spec.faq_facts:
            findings.append(
                SpecFinding(
                    source=FindingSource.COMPLETENESS,
                    severity=Severity.MAJOR,
                    requirement_ref="faq_facts",
                    detail="Raw instruction includes FAQ/knowledge section but TaskSpec has no FAQFact.",
                    suggested_fix="Parse FAQ section into FAQFact with grounding_source.",
                    decision=FindingDecision.AUTO_FIX,
                )
            )
        if ("结束" in raw_instruction or "挂断" in raw_instruction or "不方便" in raw_instruction) and not task_spec.termination_rules:
            findings.append(
                SpecFinding(
                    source=FindingSource.COMPLETENESS,
                    severity=Severity.MAJOR,
                    requirement_ref="termination_rules",
                    detail="Raw instruction mentions termination behavior but TaskSpec has no termination rule.",
                    suggested_fix="Create TerminationRule from the relevant call flow line.",
                    decision=FindingDecision.AUTO_FIX,
                )
            )
        flow_like_lines = [
            line.strip()
            for line in raw_instruction.splitlines()
            if re.match(r"^\s*(?:[-*]|\d+[.、])\s+", line) and len(line.strip()) > 8
        ]
        if flow_like_lines and len(task_spec.flow_nodes) == 0:
            findings.append(
                SpecFinding(
                    source=FindingSource.COMPLETENESS,
                    severity=Severity.CRITICAL,
                    requirement_ref="flow_nodes",
                    detail="Instruction appears to contain flow steps but TaskSpec has no flow nodes.",
                    suggested_fix="Build flow_nodes and flow requirement items from Call Flow/Conversation Flow.",
                    decision=FindingDecision.AUTO_FIX,
                )
            )
        return findings

