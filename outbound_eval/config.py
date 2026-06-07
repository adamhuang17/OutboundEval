from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_PG_DSN = "postgresql://agent_hub:agent_hub_pass@192.168.111.134:5432/outboundeval"
DEFAULT_REDIS_URL = "redis://192.168.111.134:6379/0"


@dataclass(frozen=True)
class AppSettings:
    pg_dsn: str = os.getenv("PG_DSN", DEFAULT_PG_DSN)
    redis_url: str = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
    storage_backend: str = os.getenv("OUTBOUNDEVAL_STORAGE", "postgres")


def settings() -> AppSettings:
    return AppSettings()

