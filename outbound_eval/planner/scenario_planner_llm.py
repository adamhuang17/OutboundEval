from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import Severity
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_persona import EvaluatorPersonaInput
from outbound_eval.domain.schemas_understanding import ScenarioPlan, ScenarioPlanItem, TaskUnderstanding
from outbound_eval.llm.structured_client import StructuredLLMClient


_SYSTEM_PROMPT = """你是外呼评测场景覆盖规划器。
先根据 TaskSpec/JudgePlan/RiskPlan 输出短 JSON 覆盖计划，不要生成完整话术。

严格规则：
1. 每个重要 judge_point 至少被一个 plan item 覆盖。
2. plan item 只能描述覆盖意图、用户画像焦点和关联 id。
3. 不要套用固定模板；根据当前任务语义规划真实测试角度。
4. 如任务有风险覆盖要求，必须生成 linked_risk_coverage_ids。
5. 只输出 JSON，不要有其他内容。

输出格式：
{
  "items": [
    {
      "id": "plan.001",
      "title": "string",
      "scenario_type": "main_flow|branch|knowledge_probe|constraint_probe|exception|adversarial|metamorphic",
      "coverage_intent": "string",
      "linked_judge_point_ids": ["jp.XXX"],
      "linked_requirement_ids": ["req.XXX"],
      "linked_risk_coverage_ids": [],
      "persona_focus": "string",
      "priority": "critical|major|minor"
    }
  ]
}"""


class _ScenarioPlanDraft(BaseModel):
    model_config = ConfigDict(extra="allow")
    items: list[dict[str, Any]] = Field(default_factory=list)


class ScenarioPlannerLLM:
    def __init__(self, client: StructuredLLMClient | None = None):
        from outbound_eval.llm.structured_client import get_client

        self._client = client or get_client()

    async def plan(
        self,
        *,
        understanding: TaskUnderstanding,
        persona: EvaluatorPersonaInput,
        scenario_count: int,
        model_config: ModelConfig,
    ) -> ScenarioPlan:
        task_spec = understanding.task_spec
        task_id = task_spec.get("task_id", "task_unknown")
        judge_points = "\n".join(
            f"- {jp.id}: [{jp.dimension}] severity={jp.severity} reqs={jp.linked_requirement_ids} {jp.criterion}"
            for jp in understanding.judge_plan.judge_points
        )
        requirements = "\n".join(
            f"- {req.get('id')}: [{req.get('category')}] {req.get('name')} :: {req.get('source_text', '')[:120]}"
            for req in task_spec.get("requirements", [])
        )
        risks = "\n".join(
            f"- {req.id}: risk={req.linked_risk_category_id} min={req.min_scenarios} {req.description}"
            for req in understanding.risk_plan.coverage_requirements
        )
        persona_summary = (
            f"identity={persona.identity or '未指定'}; relationship={persona.relationship_to_task or '未指定'}; "
            f"motivation={persona.motivation or '未指定'}; attitude={persona.attitude or '未指定'}; "
            f"style={persona.communication_style or '未指定'}; focus={persona.initial_focus or '未指定'}"
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""任务：{task_spec.get('task_name', '')}
目标：{task_spec.get('objective', '')}
画像：{persona_summary}
计划数量：{scenario_count}

JudgePoints:
{judge_points or '（无）'}

Requirements:
{requirements or '（无）'}

Risk coverage:
{risks or '（无）'}

请输出 {scenario_count} 个覆盖计划。""",
            },
        ]
        result = await self._client.invoke_json(
            model_config=model_config,
            messages=messages,
            output_model=_ScenarioPlanDraft,
            stage="plan_scenarios",
            temperature=0.3,
        )
        draft = result.parsed
        items: list[ScenarioPlanItem] = []
        for idx, raw in enumerate(draft.items[:scenario_count], start=1):
            items.append(
                ScenarioPlanItem(
                    id=raw.get("id", f"plan.{idx:03d}"),
                    title=raw.get("title", f"覆盖计划 {idx}"),
                    scenario_type=raw.get("scenario_type", "main_flow"),
                    coverage_intent=raw.get("coverage_intent", ""),
                    linked_judge_point_ids=raw.get("linked_judge_point_ids", []),
                    linked_requirement_ids=raw.get("linked_requirement_ids", []),
                    linked_risk_coverage_ids=raw.get("linked_risk_coverage_ids", []),
                    persona_focus=raw.get("persona_focus", ""),
                    priority=raw.get("priority", Severity.MAJOR.value),
                )
            )
        return ScenarioPlan(task_id=task_id, items=items)
