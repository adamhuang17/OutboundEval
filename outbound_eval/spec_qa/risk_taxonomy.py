from __future__ import annotations

from pathlib import Path

import yaml

from outbound_eval.domain.schemas_task import RiskCategory


class RiskTaxonomy:
    def __init__(self, categories: dict[str, RiskCategory]):
        self.categories = categories

    @classmethod
    def load(cls, path: Path | None = None) -> "RiskTaxonomy":
        path = path or Path(__file__).with_name("risk_taxonomy.yaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw_categories = data.get("risk_taxonomy", {})
        categories = {
            category_id: RiskCategory(id=category_id, **payload)
            for category_id, payload in raw_categories.items()
        }
        return cls(categories)

    def get(self, risk_category_id: str) -> RiskCategory:
        return self.categories[risk_category_id]

    def __iter__(self):
        return iter(self.categories.values())

