"""Persistence layer."""

from outbound_eval.config import settings
from outbound_eval.storage.postgres_repository import PostgresRepository
from outbound_eval.storage.sqlite_repository import SQLiteRepository


def default_repository():
    cfg = settings()
    if cfg.storage_backend == "sqlite":
        return SQLiteRepository("runs/outbound_eval.db")
    return PostgresRepository(cfg.pg_dsn)


__all__ = ["PostgresRepository", "SQLiteRepository", "default_repository"]

