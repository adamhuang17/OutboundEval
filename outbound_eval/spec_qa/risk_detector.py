from __future__ import annotations

from dataclasses import dataclass

from outbound_eval.domain.schemas_task import DetectedRisk, TaskSpec
from outbound_eval.spec_qa.risk_taxonomy import RiskTaxonomy


@dataclass
class RiskSource:
    kind: str
    id: str
    text: str


class RiskDetector:
    def __init__(self, taxonomy: RiskTaxonomy) -> None:
        self.taxonomy = taxonomy

    def detect(self, raw_instruction: str, task_spec: TaskSpec) -> list[DetectedRisk]:
        corpus = self._build_corpus(raw_instruction, task_spec)
        detected: dict[str, DetectedRisk] = {}
        for category in self.taxonomy:
            matched_terms: set[str] = set()
            req_ids: set[str] = set()
            faq_ids: set[str] = set()
            constraint_ids: set[str] = set()
            spans: list[str] = []
            for source in corpus:
                source_terms = [term for term in category.terms if term and term in source.text]
                if not source_terms:
                    continue
                matched_terms.update(source_terms)
                spans.append(f"{source.kind}:{source.id}:{source.text[:120]}")
                if source.kind == "requirement":
                    req_ids.add(source.id)
                elif source.kind == "faq":
                    faq_ids.add(source.id)
                elif source.kind == "constraint":
                    constraint_ids.add(source.id)
            if matched_terms:
                detected[category.id] = DetectedRisk(
                    risk_category_id=category.id,
                    matched_terms=sorted(matched_terms),
                    matched_requirement_ids=sorted(req_ids),
                    matched_faq_fact_ids=sorted(faq_ids),
                    matched_constraint_ids=sorted(constraint_ids),
                    source_spans=spans,
                    confidence=1.0,
                )
        return list(detected.values())

    def _build_corpus(self, raw_instruction: str, task_spec: TaskSpec) -> list[RiskSource]:
        corpus = [RiskSource("raw", "raw_instruction", raw_instruction)]
        corpus.extend(RiskSource("requirement", req.id, req.source_text) for req in task_spec.requirements)
        corpus.extend(
            RiskSource("faq", fact.id, "\n".join([fact.question, fact.answer, fact.grounding_source]))
            for fact in task_spec.faq_facts
        )
        corpus.extend(RiskSource("constraint", item.id, item.rule_text) for item in task_spec.constraints)
        corpus.extend(
            RiskSource("forbidden", item.id, "\n".join([item.name, item.description, item.source_text]))
            for item in task_spec.forbidden_behaviors
        )
        corpus.extend(
            RiskSource("termination", item.id, "\n".join([item.condition, item.source_text]))
            for item in task_spec.termination_rules
        )
        return corpus

