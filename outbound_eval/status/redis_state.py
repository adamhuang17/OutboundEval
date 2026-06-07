from __future__ import annotations

import json
from typing import Any

try:
    import redis
except ModuleNotFoundError:  # pragma: no cover - depends on local optional deps
    redis = None


class RedisStateStore:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._memory: dict[str, str] = {}
        self.client = redis.Redis.from_url(redis_url, decode_responses=True) if redis else None

    def ping(self) -> bool:
        if not self.client:
            return False
        return bool(self.client.ping())

    def set_run_status(self, run_id: str, status: dict[str, Any], ttl_seconds: int = 86400) -> None:
        key = f"outboundeval:run:{run_id}:status"
        payload = json.dumps(status, ensure_ascii=False)
        if not self.client:
            self._memory[key] = payload
            return
        self.client.set(key, payload, ex=ttl_seconds)

    def get_run_status(self, run_id: str) -> dict[str, Any] | None:
        key = f"outboundeval:run:{run_id}:status"
        raw = self.client.get(key) if self.client else self._memory.get(key)
        return json.loads(raw) if raw else None

    def cache_connection_test(self, fingerprint: str, result: dict[str, Any], ttl_seconds: int = 900) -> None:
        key = f"outboundeval:model_test:{fingerprint}"
        payload = json.dumps(result, ensure_ascii=False)
        if not self.client:
            self._memory[key] = payload
            return
        self.client.set(key, payload, ex=ttl_seconds)

    def get_cached_connection_test(self, fingerprint: str) -> dict[str, Any] | None:
        key = f"outboundeval:model_test:{fingerprint}"
        raw = self.client.get(key) if self.client else self._memory.get(key)
        return json.loads(raw) if raw else None
