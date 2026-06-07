"""LLMTaskCompiler prompt builders.

规则：
- 不假设固定业务领域，不使用任何固定行业词
- 每个 requirement / knowledge_fact / judge_point 必须引用 source_node_id
- 如信息不足，输出 compile_findings，不编造
"""
from __future__ import annotations

import json

from outbound_eval.domain.schemas_markdown import MarkdownAst
from outbound_eval.compiler.markdown_ast import MarkdownAstParser

_SYSTEM_PROMPT = """你是一个通用外呼任务指令编译器。
你需要把评测员输入的半结构化 Markdown 外呼任务说明，编译成精确的评测规约 JSON。

严格规则：
1. 不假设任何固定业务领域，只利用 Markdown 层级和语义提取信息。
2. 不新增原文没有的政策、金额、承诺、流程。
3. 如果信息不足，在 compile_findings 中说明，不要编造。
4. 每个 requirement/knowledge_fact/judge_point 必须引用对应的 source_node_id（来自 AST 节点列表）。
5. 输出严格 JSON，不要有其他说明文字。

输出格式为 JSON 对象，包含以下字段：
{
  "task_name": "string（简短任务名，不超过20字）",
  "role": "string（角色描述，如'外呼客服'）",
  "objective": "string（任务目标，1-2句话）",
  "opening_line": "string（开场白，可为空）",
  "requirements": [
    {
      "id": "req.XXX",
      "name": "string",
      "category": "task|flow|knowledge|constraint|exception|termination",
      "source_node_id": "string",
      "source_text": "string（原文引用）",
      "check_method": "rule|flow|knowledge|llm|hybrid",
      "severity": "critical|major|minor",
      "tags": []
    }
  ],
  "flow_nodes": [
    {"id": "flow.XXX", "name": "string", "instruction": "string", "requirement_ids": [], "order": 0}
  ],
  "branch_rules": [
    {"id": "branch.XXX", "name": "string", "condition": "string", "source_text": "string"}
  ],
  "knowledge_facts": [
    {
      "id": "kf.XXX",
      "text": "string",
      "fact_type": "faq|policy|business_rule|procedure|definition|constraint_detail|other",
      "source_node_id": "string",
      "source_text": "string",
      "requirement_ids": [],
      "question_patterns": [],
      "answer": "string或null"
    }
  ],
  "constraints": [
    {"id": "con.XXX", "name": "string", "rule_text": "string", "severity": "critical|major|minor"}
  ],
  "forbidden_behaviors": [
    {"id": "fb.XXX", "name": "string", "description": "string", "severity": "critical", "cap_score": 60.0}
  ],
  "termination_rules": [
    {"id": "term.XXX", "name": "string", "condition": "string", "source_text": "string"}
  ],
  "variables": [
    {"name": "string", "kind": "string", "examples": [], "source_text": "string"}
  ],
  "judge_plan": {
    "judge_points": [
      {
        "id": "jp.XXX",
        "dimension": "task_completion|flow_following|knowledge_correctness|constraint_following|exception_handling|user_experience|safety_compliance",
        "criterion": "string（评分标准描述）",
        "pass_criteria": "string（通过条件）",
        "partial_criteria": "string（部分通过条件，可为空）",
        "fail_criteria": "string（不通过条件）",
        "severity": "critical|major|minor",
        "weight": 1.0,
        "source_node_id": "string",
        "source_text": "string",
        "linked_requirement_ids": [],
        "evaluator": "rule|llm|hybrid"
      }
    ],
    "dimension_weights": {
      "task_completion": 0.25,
      "flow_following": 0.2,
      "knowledge_correctness": 0.2,
      "constraint_following": 0.15,
      "exception_handling": 0.1,
      "user_experience": 0.05,
      "safety_compliance": 0.05
    }
  },
  "risk_plan": {
    "detected_risks": [
      {
        "risk_category_id": "string",
        "description": "string",
        "severity": "critical|major|minor",
        "auto_guarded": false,
        "guard_description": "string"
      }
    ],
    "coverage_requirements": []
  },
  "compile_findings": [
    {
      "code": "string（如 MISSING_FLOW、AMBIGUOUS_CONSTRAINT）",
      "message": "string",
      "severity": "minor|major|critical",
      "blocking": false,
      "source_node_id": "string",
      "suggestion": "string"
    }
  ],
  "compiler_notes": []
}"""


def build_compiler_messages(raw_markdown: str, ast: MarkdownAst) -> list[dict[str, str]]:
    """构建 LLMTaskCompiler 的 messages 列表。"""
    parser = MarkdownAstParser()
    nodes_flat = parser.flatten(ast)

    ast_summary_lines = ["AST 节点列表（供 source_node_id 引用）："]
    for node in nodes_flat:
        prefix = "#" * node.level if node.level > 0 else "root"
        bullet_count = len(node.bullets)
        ast_summary_lines.append(
            f"  id={node.id!r} level={node.level} heading={node.heading!r} "
            f"lines={node.start_line}-{node.end_line} bullets={bullet_count}"
        )
    ast_summary = "\n".join(ast_summary_lines)

    user_content = f"""请将以下外呼任务 Markdown 编译为评测规约 JSON。

{ast_summary}

---原始 Markdown---
{raw_markdown}
---结束---

要求：
- task_name 从 Markdown 语义提取，不要使用固定业务关键词
- 每个 requirements 条目的 source_node_id 必须是上面节点列表中的真实 id
- judge_points 必须覆盖 flow、knowledge、constraint 的关键点
- 只输出 JSON，不要有任何其他内容"""

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
