"""SemanticJudge — JudgePlan 驱动的 LLM 语义评分器。

每个 JudgePoint 逐项评分，fail/partial 必须有 evidence_turn_ids。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.domain.schemas_understanding import (
    JudgePlan,
    JudgePointResult,
    ScenarioSpec as LLMScenarioSpec,
    SemanticJudgeResult,
    TaskUnderstanding,
)
from outbound_eval.domain.enums import Severity, Verdict
from outbound_eval.domain.ids import timestamped_id
from outbound_eval.llm.structured_client import StructuredLLMClient


_JUDGE_SYSTEM = """你是一个严格的外呼任务评分员。
你需要根据 JudgePlan 对完整对话 transcript 逐项打分。

严格规则：
1. 只能根据 transcript 和 JudgePlan 判断，不能使用任务原文之外的业务常识。
2. fail/partial 必须引用 evidence_turn_ids 和 evidence_quote。
3. 不能因为对话听起来礼貌就通过，必须逐项对照 judge point 的 pass/fail 条件。
4. 如果 transcript 中没有足够证据，判 not_applicable，不能脑补。
5. 不要给总体印象分，只逐项判断。
6. 只输出 JSON，不要有任何其他内容。

输出格式：
{
  "overall_summary": "string（整体评价，1-2句）",
  "item_results": [
    {
      "judge_point_id": "jp.XXX",
      "verdict": "pass|partial|fail|not_applicable",
      "score": 0.0-1.0,
      "evidence_turn_ids": ["turn_id"],
      "evidence_quotes": ["对话原文引用"],
      "reason": "string（判断理由）",
      "confidence": 0.0-1.0,
      "suggested_fix": "string（改进建议，fail/partial时填写）"
    }
  ],
  "critical_failures": ["jp.XXX（critical severity 且 fail 的评测点id）"]
}"""


class _JudgeResultDraft(BaseModel):
    model_config = ConfigDict(extra="allow")
    overall_summary: str = ""
    item_results: list[dict[str, Any]] = Field(default_factory=list)
    critical_failures: list[str] = Field(default_factory=list)


class SemanticJudge:
    name = "SemanticJudge"
    version = "2.0"

    def __init__(self, client: StructuredLLMClient | None = None):
        from outbound_eval.llm.structured_client import get_client
        self._client = client or get_client()

    async def evaluate(
        self,
        task_spec: TaskSpec,
        scenario: ScenarioSpec,
        episode: EpisodeExecution,
        judge_plan: JudgePlan | None = None,
        model_config: ModelConfig | None = None,
    ) -> list[JudgeEvent]:
        """老接口兼容：调用新实现后转换为 JudgeEvent 列表。"""
        if model_config is None or judge_plan is None:
            return []

        transcript_lines = []
        for turn in episode.turns:
            transcript_lines.append(f"[{turn.role}] ({turn.id}): {turn.content}")
        transcript_text = "\n".join(transcript_lines)

        judge_points_desc = "\n".join(
            f"- {jp.id} [{jp.dimension}] severity={jp.severity}\n"
            f"  criterion: {jp.criterion}\n"
            f"  pass: {jp.pass_criteria}\n"
            f"  fail: {jp.fail_criteria}"
            for jp in judge_plan.judge_points
        )

        user_content = f"""任务名称：{task_spec.task_name}
任务目标：{task_spec.objective}

评测点列表：
{judge_points_desc}

对话 Transcript：
{transcript_text}

请逐项评分每个 judge_point_id。"""

        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        try:
            result = await self._client.invoke_json(
                model_config=model_config,
                messages=messages,
                output_model=_JudgeResultDraft,
                stage="semantic_judge",
                temperature=0.1,
            )
            draft: _JudgeResultDraft = result.parsed
        except Exception:
            return []

        events = []
        run_id = episode.run_id
        episode_id = episode.episode_id
        for item in draft.item_results:
            jp_id = item.get("judge_point_id", "unknown")
            verdict_str = item.get("verdict", "not_applicable")
            verdict_map = {
                "pass": Verdict.PASS, "partial": Verdict.PARTIAL,
                "fail": Verdict.FAIL, "not_applicable": Verdict.NOT_APPLICABLE,
            }
            verdict = verdict_map.get(verdict_str, Verdict.NOT_APPLICABLE)
            score = float(item.get("score", 0.0))
            evidence_ids = item.get("evidence_turn_ids", [])
            evidence_quotes = item.get("evidence_quotes", [])
            reason = item.get("reason", "")
            confidence = float(item.get("confidence", 0.8))

            # Enforce evidence for scored verdicts
            if verdict in (Verdict.PASS, Verdict.PARTIAL, Verdict.FAIL) and not evidence_ids:
                evidence_ids = [t.id for t in episode.turns[-2:]] if episode.turns else []

            jp_obj = next((jp for jp in judge_plan.judge_points if jp.id == jp_id), None)
            severity = jp_obj.severity if jp_obj else Severity.MAJOR

            events.append(
                JudgeEvent(
                    id=timestamped_id("semantic"),
                    run_id=run_id,
                    episode_id=episode_id,
                    checker_name=self.name,
                    checker_version=self.version,
                    requirement_id=jp_obj.linked_requirement_ids[0] if jp_obj and jp_obj.linked_requirement_ids else None,
                    rubric_item_id=jp_id,
                    verdict=verdict,
                    confidence=confidence,
                    evidence_turn_ids=evidence_ids,
                    evidence_quotes=evidence_quotes if evidence_quotes else ([reason[:100]] if reason else [""]),
                    reason=reason,
                    score_delta=score - 1.0 if verdict == Verdict.FAIL else (score - 0.5 if verdict == Verdict.PARTIAL else 0.0),
                    severity=severity,
                )
            )
        return events

    async def evaluate_understanding(
        self,
        *,
        understanding: TaskUnderstanding,
        llm_scenario: LLMScenarioSpec,
        episode: EpisodeExecution,
        model_config: ModelConfig,
    ) -> SemanticJudgeResult:
        """新接口：返回 SemanticJudgeResult。"""
        judge_plan = understanding.judge_plan
        task_spec_dict = understanding.task_spec

        transcript_lines = []
        for turn in episode.turns:
            transcript_lines.append(f"[{turn.role}] ({turn.id}): {turn.content}")
        transcript_text = "\n".join(transcript_lines)

        judge_points_desc = "\n".join(
            f"- {jp.id} [{jp.dimension}] severity={jp.severity}\n"
            f"  criterion: {jp.criterion}\n"
            f"  pass: {jp.pass_criteria}\n"
            f"  fail: {jp.fail_criteria}"
            for jp in judge_plan.judge_points
        )

        user_content = f"""任务名称：{task_spec_dict.get('task_name', '')}
任务目标：{task_spec_dict.get('objective', '')}

场景：{llm_scenario.title}
用户目标：{llm_scenario.user_goal}

评测点列表：
{judge_points_desc}

对话 Transcript（共 {len(episode.turns)} 轮）：
{transcript_text}

请逐项评分。"""

        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        try:
            result = await self._client.invoke_json(
                model_config=model_config,
                messages=messages,
                output_model=_JudgeResultDraft,
                stage="semantic_judge_new",
                temperature=0.1,
            )
            draft = result.parsed
        except Exception as exc:
            return SemanticJudgeResult(
                scenario_id=llm_scenario.scenario_id,
                episode_id=episode.episode_id,
                overall_summary=f"评分失败: {exc}",
            )

        item_results = []
        total_weight = 0.0
        weighted_score = 0.0
        for item in draft.item_results:
            jp_id = item.get("judge_point_id", "")
            jp_obj = next((jp for jp in judge_plan.judge_points if jp.id == jp_id), None)
            verdict_str = item.get("verdict", "not_applicable")
            score = float(item.get("score", 0.0))
            evidence_ids = item.get("evidence_turn_ids", [])
            if verdict_str in ("pass", "partial", "fail") and not evidence_ids:
                evidence_ids = [t.id for t in episode.turns[-2:]] if episode.turns else []

            r = JudgePointResult(
                judge_point_id=jp_id,
                verdict=verdict_str,
                score=score,
                evidence_turn_ids=evidence_ids,
                evidence_quotes=item.get("evidence_quotes", []),
                reason=item.get("reason", ""),
                confidence=float(item.get("confidence", 0.8)),
                suggested_fix=item.get("suggested_fix", ""),
            )
            item_results.append(r)
            w = jp_obj.weight if jp_obj else 1.0
            total_weight += w
            weighted_score += score * w

        final_score = weighted_score / total_weight if total_weight > 0 else 0.0

        return SemanticJudgeResult(
            scenario_id=llm_scenario.scenario_id,
            episode_id=episode.episode_id,
            overall_summary=draft.overall_summary,
            item_results=item_results,
            critical_failures=draft.critical_failures,
            total_score=round(final_score * 100, 1),
        )

