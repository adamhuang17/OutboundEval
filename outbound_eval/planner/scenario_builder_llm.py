"""ScenarioBuilderLLM — 根据 TaskUnderstanding 和评测员画像，用 LLM 生成测试场景。"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_persona import EvaluatorPersonaInput
from outbound_eval.domain.schemas_understanding import (
    JudgePlan,
    PersonaSpec,
    ScenarioSet,
    ScenarioSpec,
    TaskUnderstanding,
)
from outbound_eval.llm.structured_client import StructuredLLMClient


_SYSTEM_PROMPT = """你是一个外呼任务场景构建器。
根据任务规约和评测员提供的模拟用户画像，生成多组能有效测试任务执行质量的场景。

严格规则：
1. 每个场景必须绑定 linked_judge_point_ids（来自 JudgePlan）。
2. 场景要贴合当前任务，不允许只输出 happy_path/refusal/driving 这类通用模板。
3. 评测员画像必须进入 persona 和 hidden_user_goal。
4. 必须包含 initial_user_utterance 和 dialogue_direction。
5. 不要把 expected_model_behavior、hidden_user_goal 泄露给被测模型。
6. 如果任务没有某类内容，不要编造相应场景。
7. 只输出 JSON，不要有任何其他内容。

输出格式：
{
  "scenarios": [
    {
      "scenario_id": "scn_001",
      "title": "场景标题",
      "scenario_type": "main_flow|branch|knowledge_probe|constraint_probe|exception|adversarial|metamorphic",
      "persona": {
        "identity": "string",
        "relationship_to_task": "string",
        "motivation": "string",
        "attitude": "string",
        "communication_style": "string",
        "initial_focus": "string",
        "decision_rule": "string",
        "inconvenience_context": "string"
      },
      "user_goal": "string（用户表面目标）",
      "hidden_user_goal": "string（隐藏测试目的，不暴露给被测模型）",
      "initial_user_utterance": "string（第一句话）",
      "dialogue_direction": ["string（每步对话推进方向）"],
      "expected_model_behavior": ["string（期望模型行为）"],
      "forbidden_behavior": ["string（禁止模型行为）"],
      "stop_conditions": ["string（对话终止条件）"],
      "linked_judge_point_ids": ["jp.XXX"],
      "covered_requirement_ids": ["req.XXX"],
      "max_turns": 8
    }
  ]
}"""


class _ScenarioSetDraft(BaseModel):
    model_config = ConfigDict(extra="allow")
    scenarios: list[dict[str, Any]] = Field(default_factory=list)


class ScenarioBuilderLLM:
    """LLM 驱动的场景构建器。"""

    def __init__(self, client: StructuredLLMClient | None = None):
        from outbound_eval.llm.structured_client import get_client
        self._client = client or get_client()

    async def build(
        self,
        *,
        understanding: TaskUnderstanding,
        persona: EvaluatorPersonaInput,
        scenario_count: int = 6,
        model_config: ModelConfig,
    ) -> ScenarioSet:
        task_spec = understanding.task_spec
        task_id = task_spec.get("task_id", "task_unknown")
        judge_points = understanding.judge_plan.judge_points
        knowledge_facts = understanding.knowledge_facts

        jp_summary = "\n".join(
            f"- {jp.id}: [{jp.dimension}] {jp.criterion} (severity={jp.severity})"
            for jp in judge_points[:20]
        )
        req_summary = "\n".join(
            f"- {r.get('id', '')}: {r.get('name', '')} [{r.get('category', '')}]"
            for r in task_spec.get("requirements", [])[:15]
        )
        kf_summary = "\n".join(
            f"- {kf.id}: [{kf.fact_type}] {kf.text[:80]}"
            for kf in knowledge_facts[:10]
        )
        persona_desc = f"""评测员画像：
身份: {persona.identity or '未指定'}
关系: {persona.relationship_to_task or '未指定'}
动机: {persona.motivation or '未指定'}
态度: {persona.attitude or '未指定'}
沟通风格: {persona.communication_style or '未指定'}
先关注: {persona.initial_focus or '未指定'}
决策规则: {persona.decision_rule or '未指定'}
不便上下文: {persona.inconvenience_context or '无'}
备注: {persona.extra_notes or '无'}"""

        user_content = f"""任务名称：{task_spec.get('task_name', '未命名')}
任务目标：{task_spec.get('objective', '')}
角色：{task_spec.get('role', '')}

评测点（JudgePoints，场景必须覆盖这些）：
{jp_summary or '（无）'}

需求列表：
{req_summary or '（无）'}

知识点：
{kf_summary or '（无）'}

{persona_desc}

场景数量：{scenario_count}

请生成 {scenario_count} 个测试场景，覆盖 critical 评测点，并融合上面的用户画像。"""

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        result = await self._client.invoke_json(
            model_config=model_config,
            messages=messages,
            output_model=_ScenarioSetDraft,
            stage="build_scenarios",
            temperature=0.5,
        )
        draft: _ScenarioSetDraft = result.parsed

        scenarios = []
        for raw in draft.scenarios:
            raw_persona = raw.get("persona", {})
            spec = ScenarioSpec(
                scenario_id=raw.get("scenario_id", f"scn_{uuid.uuid4().hex[:6]}"),
                task_id=task_id,
                title=raw.get("title", "无标题场景"),
                scenario_type=raw.get("scenario_type", "main_flow"),
                persona=PersonaSpec(
                    identity=raw_persona.get("identity", persona.identity),
                    relationship_to_task=raw_persona.get("relationship_to_task", persona.relationship_to_task),
                    motivation=raw_persona.get("motivation", persona.motivation),
                    attitude=raw_persona.get("attitude", persona.attitude),
                    communication_style=raw_persona.get("communication_style", persona.communication_style),
                    initial_focus=raw_persona.get("initial_focus", persona.initial_focus),
                    decision_rule=raw_persona.get("decision_rule", persona.decision_rule),
                    inconvenience_context=raw_persona.get("inconvenience_context", persona.inconvenience_context),
                ),
                user_goal=raw.get("user_goal", ""),
                hidden_user_goal=raw.get("hidden_user_goal", ""),
                initial_user_utterance=raw.get("initial_user_utterance", "您好"),
                dialogue_direction=raw.get("dialogue_direction", []),
                expected_model_behavior=raw.get("expected_model_behavior", []),
                forbidden_behavior=raw.get("forbidden_behavior", []),
                stop_conditions=raw.get("stop_conditions", ["用户明确结束"]),
                linked_judge_point_ids=raw.get("linked_judge_point_ids", []),
                covered_requirement_ids=raw.get("covered_requirement_ids", []),
                max_turns=int(raw.get("max_turns", 8)),
            )
            scenarios.append(spec)

        return ScenarioSet(task_id=task_id, scenarios=scenarios)
