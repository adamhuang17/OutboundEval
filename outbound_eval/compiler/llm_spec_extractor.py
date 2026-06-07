from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.compiler.section_splitter import section_map


class TaskSpecDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_name: str
    role: str
    objective: str
    opening_line: str = ""
    flow_steps: list[str] = Field(default_factory=list)
    faq_pairs: list[tuple[str, str]] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    raw_sections: dict[str, str] = Field(default_factory=dict)


def _lines(body: str) -> list[str]:
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip(" \t-*0123456789.、")
        if line:
            out.append(line)
    return out


def _parse_faq(body: str) -> list[tuple[str, str]]:
    lines = _lines(body)
    pairs: list[tuple[str, str]] = []
    current_q: str | None = None
    for line in lines:
        if re.match(r"^(q|问|问题)[:：]", line, flags=re.I) or line.endswith("?") or line.endswith("？"):
            current_q = re.sub(r"^(q|问|问题)[:：]\s*", "", line, flags=re.I)
            continue
        if current_q:
            answer = re.sub(r"^(a|答|答案)[:：]\s*", "", line, flags=re.I)
            pairs.append((current_q, answer))
            current_q = None
        elif "：" in line or ":" in line:
            q, a = re.split(r"[:：]", line, maxsplit=1)
            if q.strip() and a.strip():
                pairs.append((q.strip(), a.strip()))
    return pairs


class RuleBasedSpecExtractor:
    """Produces TaskSpecDraft only; final TaskSpec is built by normalizer/validators."""

    def extract(self, raw_instruction: str) -> TaskSpecDraft:
        sections = section_map(raw_instruction)
        role = sections.get("role", "").strip()
        objective = sections.get("task", "").strip()
        opening_line = sections.get("opening_line", "").strip().strip('"')
        task_name = self._derive_task_name(role, objective, raw_instruction)
        flow_steps = _lines(sections.get("call_flow", ""))
        constraints = _lines(sections.get("constraints", ""))
        faq_pairs = _parse_faq(sections.get("faq", ""))
        return TaskSpecDraft(
            task_name=task_name,
            role=role or "Outbound call specialist",
            objective=objective or task_name,
            opening_line=opening_line,
            flow_steps=flow_steps,
            faq_pairs=faq_pairs,
            constraints=constraints,
            raw_sections=sections,
        )

    def _derive_task_name(self, role: str, objective: str, raw: str) -> str:
        text = " ".join(part for part in [role, objective] if part).strip()
        if "骑手" in raw or "飞毛腿" in raw:
            return "美团外卖骑手飞毛腿合同通知任务"
        if "直播" in raw or "课程" in raw:
            return "课程发布平台低延迟直播选项通知任务"
        if text:
            return text[:60]
        return "Outbound Evaluation Task"

