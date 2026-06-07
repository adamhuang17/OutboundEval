from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import psycopg

from outbound_eval.domain.enums import EventType
from outbound_eval.domain.schemas_episode import EpisodeExecution, ModelCallEvent, TurnEvent
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.trace.store import TraceEvent


class PostgresTraceStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def append(self, event: TraceEvent) -> None:
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into trace_events
                    (id, event_type, run_id, episode_id, requirement_id, span_id, parent_span_id, payload_json, created_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    on conflict (id) do update
                    set payload_json = excluded.payload_json
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
                        event.created_at,
                    ),
                )
            conn.commit()

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
            clauses.append("run_id = %s")
            params.append(run_id)
        if episode_id:
            clauses.append("episode_id = %s")
            params.append(episode_id)
        if requirement_id:
            clauses.append("requirement_id = %s")
            params.append(requirement_id)
        if event_type:
            clauses.append("event_type = %s")
            params.append(event_type.value if hasattr(event_type, "value") else str(event_type))
        where = (" where " + " and ".join(clauses)) if clauses else ""
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select id, event_type, run_id, episode_id, requirement_id, span_id, parent_span_id, payload_json, created_at from trace_events{where} order by created_at, id",
                    params,
                )
                rows = cur.fetchall()
        return [
            TraceEvent(
                id=row[0],
                event_type=row[1],
                run_id=row[2],
                episode_id=row[3],
                requirement_id=row[4],
                span_id=row[5],
                parent_span_id=row[6],
                payload=row[7],
                created_at=row[8] if isinstance(row[8], datetime) else datetime.fromisoformat(row[8]),
            )
            for row in rows
        ]

