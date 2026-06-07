from __future__ import annotations

from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_understanding import JudgePointResult, SemanticJudgeResult


class EvidenceMapper:
    """Attach replayable turn quotes to semantic judge results."""

    def map_semantic_result(self, episode: EpisodeExecution, result: SemanticJudgeResult) -> SemanticJudgeResult:
        turn_by_id = {turn.id: turn for turn in episode.turns}
        mapped_items: list[JudgePointResult] = []
        for item in result.item_results:
            quotes = list(item.evidence_quotes)
            if not quotes and item.evidence_turn_ids:
                quotes = [turn_by_id[turn_id].content for turn_id in item.evidence_turn_ids if turn_id in turn_by_id]
            mapped_items.append(item.model_copy(update={"evidence_quotes": quotes}))
        return result.model_copy(update={"item_results": mapped_items})

    def transcript_index(self, episode: EpisodeExecution) -> dict[str, dict[str, str | int]]:
        return {
            turn.id: {
                "role": str(turn.role),
                "turn_index": turn.turn_index,
                "content": turn.content,
            }
            for turn in episode.turns
        }
