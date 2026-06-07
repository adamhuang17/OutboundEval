from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.ids import stable_hash
from outbound_eval.domain.schemas_judge import JudgeEvent, SpecFinding
from outbound_eval.domain.schemas_understanding import SemanticJudgeResult


class AggregatedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    ref_id: str = ""
    severity: str = "major"
    verdict: str = ""
    detail: str
    evidence_turn_ids: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FindingAggregator:
    """Normalize and dedupe findings from rule checks, semantic judge, and QA gates."""

    def merge(
        self,
        *,
        rule_events: list[JudgeEvent] | None = None,
        semantic_results: list[SemanticJudgeResult] | None = None,
        qa_findings: list[SpecFinding] | None = None,
    ) -> list[AggregatedFinding]:
        items: list[AggregatedFinding] = []
        for event in rule_events or []:
            items.append(
                AggregatedFinding(
                    id=self._id("rule", event.requirement_id or event.rubric_item_id or "", str(event.verdict), event.reason),
                    source=event.checker_name,
                    ref_id=event.requirement_id or event.rubric_item_id or "",
                    severity=str(event.severity),
                    verdict=str(event.verdict),
                    detail=event.reason,
                    evidence_turn_ids=event.evidence_turn_ids,
                    evidence_quotes=event.evidence_quotes,
                    metadata=event.raw_output or {},
                )
            )
        for result in semantic_results or []:
            for item in result.item_results:
                if item.verdict in {"pass", "not_applicable"}:
                    continue
                items.append(
                    AggregatedFinding(
                        id=self._id("semantic", item.judge_point_id, item.verdict, item.reason),
                        source="SemanticJudge",
                        ref_id=item.judge_point_id,
                        severity="major",
                        verdict=item.verdict,
                        detail=item.reason,
                        evidence_turn_ids=item.evidence_turn_ids,
                        evidence_quotes=item.evidence_quotes,
                        suggested_fix=item.suggested_fix,
                        metadata={"confidence": item.confidence},
                    )
                )
        for finding in qa_findings or []:
            if finding.dismissed:
                continue
            items.append(
                AggregatedFinding(
                    id=self._id("qa", finding.requirement_ref or "", str(finding.severity), finding.detail),
                    source=str(finding.source),
                    ref_id=finding.requirement_ref or "",
                    severity=str(finding.severity),
                    verdict="blocking" if finding.blocking else "finding",
                    detail=finding.detail,
                    suggested_fix=finding.suggested_fix,
                    metadata=finding.metadata,
                )
            )
        return self._dedupe(items)

    def _dedupe(self, items: list[AggregatedFinding]) -> list[AggregatedFinding]:
        merged: dict[tuple[str, str, str], AggregatedFinding] = {}
        for item in items:
            key = (item.source, item.ref_id, item.detail.strip().lower()[:120])
            if key not in merged:
                merged[key] = item
                continue
            existing = merged[key]
            existing.evidence_turn_ids = sorted({*existing.evidence_turn_ids, *item.evidence_turn_ids})
            existing.evidence_quotes = list(dict.fromkeys([*existing.evidence_quotes, *item.evidence_quotes]))
            existing.metadata.update(item.metadata)
        return list(merged.values())

    def _id(self, source: str, ref: str, verdict: str, detail: str) -> str:
        return f"finding.{stable_hash(source + ref + verdict + detail)[:10]}"
