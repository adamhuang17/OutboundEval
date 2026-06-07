from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


TABLES = [
    "task_definitions",
    "task_understandings",
    "compile_stage_results",
    "compile_artifacts",
    "compile_diagnostics",
    "persona_profiles",
    "task_specs",
    "scenario_definitions",
    "evaluation_runs",
    "episode_executions",
    "turn_events",
    "judge_events",
    "score_items",
    "report_artifacts",
    "badcase_items",
    "golden_cases",
    "golden_labels",
]


class SQLiteRepository:
    def __init__(self, path: Path | str = "runs/outbound_eval.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def upsert_json(self, table: str, item_id: str, payload: dict[str, Any]) -> None:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        with sqlite3.connect(self.path) as db:
            db.execute(
                f"insert or replace into {table} (id, payload_json) values (?, ?)",
                (item_id, json.dumps(payload, ensure_ascii=False)),
            )

    def get_json(self, table: str, item_id: str) -> dict[str, Any] | None:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        with sqlite3.connect(self.path) as db:
            row = db.execute(f"select payload_json from {table} where id = ?", (item_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete_json(self, table: str, item_id: str) -> bool:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(f"delete from {table} where id = ?", (item_id,))
            return cursor.rowcount > 0

    def list_json(self, table: str) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        with sqlite3.connect(self.path) as db:
            rows = db.execute(f"select payload_json from {table} order by rowid desc").fetchall()
        return [json.loads(row[0]) for row in rows]

    def _init(self) -> None:
        with sqlite3.connect(self.path) as db:
            for table in TABLES:
                db.execute(
                    f"""
                    create table if not exists {table} (
                        id text primary key,
                        payload_json text not null,
                        updated_at text default current_timestamp
                    )
                    """
                )
