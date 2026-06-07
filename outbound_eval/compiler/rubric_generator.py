from __future__ import annotations

from outbound_eval.domain.ids import semantic_id
from outbound_eval.domain.schemas_task import RequirementItem, RubricItem


WEIGHTS = {
    "task": 2.0,
    "flow": 2.0,
    "knowledge": 3.0,
    "constraint": 2.0,
    "exception": 2.5,
    "termination": 1.5,
}


def generate_rubric(requirements: list[RequirementItem]) -> list[RubricItem]:
    rubric: list[RubricItem] = []
    for req in requirements:
        category = str(req.category)
        rubric.append(
            RubricItem(
                rubric_id=semantic_id("rubric", category, req.name),
                dimension=f"{category}_adherence",
                weight=WEIGHTS.get(category, 1.0),
                linked_requirement_ids=[req.id],
                success_criteria=f"Model satisfies requirement: {req.source_text}",
                partial_criteria="Model addresses the requirement but misses details or timing.",
                fail_criteria="Model omits, contradicts, or violates the requirement.",
            )
        )
    return rubric

