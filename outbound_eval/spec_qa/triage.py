from __future__ import annotations

from outbound_eval.domain.enums import FindingDecision, Severity
from outbound_eval.domain.schemas_judge import SpecFinding


class SpecQATriage:
    def normalize(self, findings: list[SpecFinding]) -> list[SpecFinding]:
        normalized: list[SpecFinding] = []
        for index, finding in enumerate(findings, start=1):
            data = finding.model_copy()
            data.id = data.id or f"finding_{index:03d}"
            if data.severity == Severity.NONE:
                data.decision = FindingDecision.DISMISS
                data.dismissed = True
            normalized.append(data)
        return normalized

    def dedupe(self, findings: list[SpecFinding]) -> list[SpecFinding]:
        merged: dict[tuple[str, str | None, str], SpecFinding] = {}
        for finding in findings:
            key = (str(finding.source), finding.requirement_ref, finding.detail.lower().strip()[:80])
            if key not in merged:
                merged[key] = finding
                continue
            existing = merged[key]
            if str(finding.source) not in str(existing.source):
                existing.detail = existing.detail + "\n" + finding.detail
            existing.metadata = self._merge_metadata(existing.metadata, finding.metadata)
            existing.blocking = existing.blocking or finding.blocking
        return list(merged.values())

    def classify(self, findings: list[SpecFinding]) -> list[SpecFinding]:
        classified: list[SpecFinding] = []
        for finding in findings:
            item = finding.model_copy()
            if item.decision == FindingDecision.DISMISS:
                item.dismissed = True
            elif item.decision == FindingDecision.AUTO_GUARDED:
                item.blocking = False
            elif item.severity == Severity.CRITICAL and item.decision == FindingDecision.DEFER:
                item.decision = FindingDecision.HUMAN_NEEDED
            classified.append(item)
        return classified

    def active(self, findings: list[SpecFinding]) -> list[SpecFinding]:
        return [finding for finding in findings if not finding.dismissed and finding.decision != FindingDecision.DISMISS]

    def _merge_metadata(self, left: dict, right: dict) -> dict:
        merged = dict(left)
        for key, value in right.items():
            if key not in merged:
                merged[key] = value
                continue
            if isinstance(merged[key], list) and isinstance(value, list):
                merged[key] = sorted({*merged[key], *value}, key=str)
            elif isinstance(merged[key], dict) and isinstance(value, dict):
                nested = dict(merged[key])
                nested.update(value)
                merged[key] = nested
        return merged
