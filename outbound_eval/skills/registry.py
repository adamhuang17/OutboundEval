from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.ids import semantic_id
from outbound_eval.domain.schemas_task import FAQFact, RubricItem, TaskSpec


class SkillPackMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    path: str
    title: str
    keywords: list[str] = Field(default_factory=list)


class SkillPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: SkillPackMeta
    task_schema: dict[str, Any] = Field(default_factory=dict)
    faq_markdown: str = ""
    persona_bank: list[dict[str, Any]] = Field(default_factory=list)
    scenario_templates: list[dict[str, Any]] = Field(default_factory=list)
    rubric: list[dict[str, Any]] = Field(default_factory=list)
    judge_prompts: str = ""


class SkillMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    confidence: float
    matched_keywords: list[str] = Field(default_factory=list)


class SkillPackRegistry:
    def __init__(self, root: Path | str = "eval_skills"):
        self.root = Path(root)

    def discover(self, root: Path | None = None) -> list[SkillPackMeta]:
        root = root or self.root
        if not root.exists():
            return []
        metas: list[SkillPackMeta] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            text = skill_md.read_text(encoding="utf-8")
            title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), child.name)
            keywords = self._extract_keywords(text)
            metas.append(SkillPackMeta(skill_id=child.name, path=str(child), title=title, keywords=keywords))
        return metas

    def load(self, skill_id: str) -> SkillPack:
        path = self.root / skill_id
        if not path.exists():
            raise FileNotFoundError(f"skill pack not found: {skill_id}")
        skill_md = (path / "SKILL.md").read_text(encoding="utf-8")
        meta = SkillPackMeta(
            skill_id=skill_id,
            path=str(path),
            title=next((line.lstrip("# ").strip() for line in skill_md.splitlines() if line.startswith("#")), skill_id),
            keywords=self._extract_keywords(skill_md),
        )
        return SkillPack(
            meta=meta,
            task_schema=self._read_yaml(path / "task_schema.yaml", {}),
            faq_markdown=self._read_text(path / "faq.md"),
            persona_bank=self._read_yaml(path / "persona_bank.yaml", []),
            scenario_templates=self._read_yaml(path / "scenario_templates.yaml", []),
            rubric=self._read_yaml(path / "rubric.yaml", []),
            judge_prompts=self._read_text(path / "judge_prompts.md"),
        )

    def match(self, task_spec: TaskSpec) -> list[SkillMatch]:
        haystack = "\n".join([task_spec.task_name, task_spec.role, task_spec.objective, task_spec.source_text]).lower()
        matches: list[SkillMatch] = []
        for meta in self.discover():
            matched = [kw for kw in meta.keywords if kw.lower() in haystack]
            confidence = min(1.0, len(matched) / max(3, len(meta.keywords) * 0.45))
            if confidence >= 0.6:
                matches.append(SkillMatch(skill_id=meta.skill_id, confidence=confidence, matched_keywords=matched))
        return sorted(matches, key=lambda item: item.confidence, reverse=True)

    def merge_defaults(self, task_spec: TaskSpec, pack: SkillPack) -> TaskSpec:
        existing_faq = {fact.question for fact in task_spec.faq_facts}
        faq_facts = list(task_spec.faq_facts)
        for question, answer in self._faq_pairs(pack.faq_markdown):
            if question in existing_faq:
                continue
            faq_facts.append(
                FAQFact(
                    id=semantic_id("faq", "knowledge", question),
                    question=question,
                    answer=answer,
                    grounding_source=f"{pack.meta.skill_id}/faq.md: {question}: {answer}",
                    requirement_ids=[],
                )
            )
        rubric = list(task_spec.rubric)
        req_ids = {req.id for req in task_spec.requirements}
        existing_rubric = {item.rubric_id for item in rubric}
        for item in pack.rubric:
            linked = [rid for rid in item.get("linked_requirement_ids", []) if rid in req_ids]
            if not linked:
                continue
            rubric_id = item.get("rubric_id") or semantic_id("rubric", item.get("dimension", "skill"), pack.meta.skill_id)
            if rubric_id in existing_rubric:
                continue
            rubric.append(
                RubricItem(
                    rubric_id=rubric_id,
                    dimension=item.get("dimension", "skill_default"),
                    weight=float(item.get("weight", 1.0)),
                    linked_requirement_ids=linked,
                    success_criteria=item.get("success_criteria", "Satisfy linked skill requirement."),
                    partial_criteria=item.get("partial_criteria", ""),
                    fail_criteria=item.get("fail_criteria", ""),
                )
            )
        return task_spec.model_copy(update={"faq_facts": faq_facts, "rubric": rubric})

    def _extract_keywords(self, text: str) -> list[str]:
        match = re.search(r"keywords\s*:\s*(.+)", text, flags=re.I)
        if not match:
            return []
        return [part.strip() for part in re.split(r"[,，/]", match.group(1)) if part.strip()]

    def _faq_pairs(self, text: str) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        current_q: str | None = None
        for raw in text.splitlines():
            line = raw.strip(" -*")
            if not line:
                continue
            if line.startswith("Q:") or line.startswith("问："):
                current_q = line.split(":", 1)[-1] if ":" in line else line.split("：", 1)[-1]
            elif current_q and (line.startswith("A:") or line.startswith("答：")):
                answer = line.split(":", 1)[-1] if ":" in line else line.split("：", 1)[-1]
                pairs.append((current_q.strip(), answer.strip()))
                current_q = None
        return pairs

    def _read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _read_yaml(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return yaml.safe_load(path.read_text(encoding="utf-8")) or default

