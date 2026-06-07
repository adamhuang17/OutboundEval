from __future__ import annotations

import json
from typing import Any

import psycopg

from outbound_eval.storage.sqlite_repository import TABLES


class PostgresRepository:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def init_db(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                for table in TABLES:
                    cur.execute(
                        f"""
                        create table if not exists {table} (
                            id text primary key,
                            payload_json jsonb not null,
                            updated_at timestamptz not null default now()
                        )
                        """
                    )
                cur.execute(
                    """
                    create table if not exists trace_events (
                        id text primary key,
                        event_type text not null,
                        run_id text not null,
                        episode_id text,
                        requirement_id text,
                        span_id text,
                        parent_span_id text,
                        payload_json jsonb not null,
                        created_at timestamptz not null default now()
                    )
                    """
                )
                cur.execute("create index if not exists idx_trace_run on trace_events(run_id)")
                cur.execute("create index if not exists idx_trace_episode on trace_events(episode_id)")
                cur.execute("create index if not exists idx_trace_requirement on trace_events(requirement_id)")
            conn.commit()

    def upsert_json(self, table: str, item_id: str, payload: dict[str, Any]) -> None:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into {table} (id, payload_json, updated_at)
                    values (%s, %s::jsonb, now())
                    on conflict (id) do update
                    set payload_json = excluded.payload_json, updated_at = now()
                    """,
                    (item_id, json.dumps(payload, ensure_ascii=False)),
                )
            conn.commit()

    def get_json(self, table: str, item_id: str) -> dict[str, Any] | None:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"select payload_json from {table} where id = %s", (item_id,))
                row = cur.fetchone()
        return row[0] if row else None

    def list_json(self, table: str, limit: int = 50) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"select payload_json from {table} order by updated_at desc limit %s", (limit,))
                rows = cur.fetchall()
        return [row[0] for row in rows]

