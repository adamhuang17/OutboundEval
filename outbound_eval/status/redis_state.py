from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import redis


class RedisStateStore:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        return bool(self.client.ping())

    def set_run_status(self, run_id: str, status: dict[str, Any], ttl_seconds: int = 86400) -> None:
        self.client.set(f"outboundeval:run:{run_id}:status", json.dumps(status, ensure_ascii=False), ex=ttl_seconds)

    def get_run_status(self, run_id: str) -> dict[str, Any] | None:
        raw = self.client.get(f"outboundeval:run:{run_id}:status")
        return json.loads(raw) if raw else None

    def cache_connection_test(self, fingerprint: str, result: dict[str, Any], ttl_seconds: int = 900) -> None:
        self.client.set(f"outboundeval:model_test:{fingerprint}", json.dumps(result, ensure_ascii=False), ex=ttl_seconds)

    def get_cached_connection_test(self, fingerprint: str) -> dict[str, Any] | None:
        raw = self.client.get(f"outboundeval:model_test:{fingerprint}")
        return json.loads(raw) if raw else None

