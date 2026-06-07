from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.domain.enums import EventType
from outbound_eval.domain.schemas_episode import EpisodeExecution, ModelCallEvent, TurnEvent
from outbound_eval.domain.schemas_judge import JudgeEvent


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: str
    event_type: EventType
    run_id: str
    episode_id: str | None = None
    requirement_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SQLiteTraceStore:
    def __init__(self, path: Path | str = "runs/outbound_eval.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def append(self, event: TraceEvent) -> None:
        with self._connect() as db:
            db.execute(
                """
                insert into trace_events
                (id, event_type, run_id, episode_id, requirement_id, span_id, parent_span_id, payload_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
                    event.run_id,
                    event.episode_id,
                    event.requirement_id,
                    event.span_id,
                    event.parent_span_id,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at.isoformat(),
                ),
            )

    def write_turn(self, turn: TurnEvent) -> None:
        for req_id in turn.related_requirement_ids or [None]:
            self.append(
                TraceEvent(
                    id=f"trace.{turn.id}.{req_id or 'none'}",
                    event_type=EventType.TURN,
                    run_id=turn.run_id,
                    episode_id=turn.episode_id,
                    requirement_id=req_id,
                    span_id=turn.id,
                    payload=turn.model_dump(mode="json"),
                )
            )

    def write_model_call(self, call: ModelCallEvent) -> None:
        self.append(
            TraceEvent(
                id=f"trace.{call.id}",
                event_type=EventType.MODEL_CALL,
                run_id=call.run_id,
                episode_id=call.episode_id,
                span_id=call.id,
                payload=call.model_dump(mode="json"),
            )
        )

    def write_judge(self, judge: JudgeEvent) -> None:
        self.append(
            TraceEvent(
                id=f"trace.{judge.id}",
                event_type=EventType.JUDGE,
                run_id=judge.run_id,
                episode_id=judge.episode_id,
                requirement_id=judge.requirement_id,
                span_id=judge.id,
                payload=judge.model_dump(mode="json"),
            )
        )

    def write_episode(self, episode: EpisodeExecution) -> None:
        self.append(
            TraceEvent(
                id=f"trace.{episode.episode_id}.execution",
                event_type=EventType.EPISODE_STARTED,
                run_id=episode.run_id,
                episode_id=episode.episode_id,
                span_id=episode.episode_id,
                payload=episode.model_dump(mode="json"),
            )
        )

    def query(
        self,
        run_id: str | None = None,
        episode_id: str | None = None,
        requirement_id: str | None = None,
        event_type: EventType | None = None,
    ) -> list[TraceEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        if requirement_id:
            clauses.append("requirement_id = ?")
            params.append(requirement_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type.value if hasattr(event_type, "value") else str(event_type))
        where = (" where " + " and ".join(clauses)) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"select id, event_type, run_id, episode_id, requirement_id, span_id, parent_span_id, payload_json, created_at from trace_events{where} order by created_at, id",
                params,
            ).fetchall()
        events: list[TraceEvent] = []
        for row in rows:
            events.append(
                TraceEvent(
                    id=row[0],
                    event_type=row[1],
                    run_id=row[2],
                    episode_id=row[3],
                    requirement_id=row[4],
                    span_id=row[5],
                    parent_span_id=row[6],
                    payload=json.loads(row[7]),
                    created_at=datetime.fromisoformat(row[8]),
                )
            )
        return events

    def _connect(self):
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                create table if not exists trace_events (
                    id text primary key,
                    event_type text not null,
                    run_id text not null,
                    episode_id text,
                    requirement_id text,
                    span_id text,
                    parent_span_id text,
                    payload_json text not null,
                    created_at text not null
                )
                """
            )
            db.execute("create index if not exists idx_trace_run on trace_events(run_id)")
            db.execute("create index if not exists idx_trace_episode on trace_events(episode_id)")
            db.execute("create index if not exists idx_trace_requirement on trace_events(requirement_id)")
