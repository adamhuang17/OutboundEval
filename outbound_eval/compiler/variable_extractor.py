from __future__ import annotations

import re

from outbound_eval.domain.schemas_task import TaskVariable


VARIABLE_PATTERNS = [
    (re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}"), "template"),
    (re.compile(r"(?<![A-Za-z0-9_])([XYZW])(?!(?:[A-Za-z0-9_]))"), "placeholder"),
    (re.compile(r"([0-9]+(?:\.[0-9]+)?\s*(?:元|块|分钟|小时|天|单|秒))"), "numeric"),
]

CHINESE_VARIABLE_HINTS = {
    "金额": "money",
    "费用": "money",
    "时间": "time",
    "数量": "count",
    "次数": "count",
    "天数": "days",
}


def extract_variables(text: str) -> list[TaskVariable]:
    seen: dict[str, TaskVariable] = {}
    for pattern, kind in VARIABLE_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1)
            seen.setdefault(
                name,
                TaskVariable(name=name, kind=kind, examples=[match.group(0)], source_text=match.group(0)),
            )
    for hint, kind in CHINESE_VARIABLE_HINTS.items():
        if hint in text and hint not in seen:
            seen[hint] = TaskVariable(name=hint, kind=kind, examples=[hint], source_text=hint)
    if "+$" in text and "positive_amount" not in seen:
        seen["positive_amount"] = TaskVariable(
            name="positive_amount", kind="money", examples=["+$"], source_text="+$"
        )
    return sorted(seen.values(), key=lambda item: item.name)
