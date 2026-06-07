"""Trace event store."""

from outbound_eval.config import settings
from outbound_eval.trace.postgres_store import PostgresTraceStore
from outbound_eval.trace.store import SQLiteTraceStore, TraceEvent


def default_trace_store():
    cfg = settings()
    if cfg.storage_backend == "sqlite":
        return SQLiteTraceStore("runs/outbound_eval.db")
    return PostgresTraceStore(cfg.pg_dsn)


__all__ = ["PostgresTraceStore", "SQLiteTraceStore", "TraceEvent", "default_trace_store"]
