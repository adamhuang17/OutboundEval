from __future__ import annotations

import hashlib
import time
from typing import Any

from openai import AsyncOpenAI

from outbound_eval.domain.ids import timestamped_id
from outbound_eval.domain.schemas_episode import ModelTurn
from outbound_eval.domain.schemas_model import ConnectionTestResult, ModelConfig, SessionHandle
from outbound_eval.domain.schemas_task import TaskSpec


class OpenAICompatibleAdapter:
    name = "openai_compatible"

    async def test_connection(self, config: ModelConfig) -> ConnectionTestResult:
        started = time.perf_counter()
        try:
            client = self._client(config)
            response = await client.chat.completions.create(
                model=config.model_name,
                messages=[{"role": "user", "content": "请回复 OK"}],
                temperature=0,
                max_tokens=8,
                timeout=config.timeout_seconds,
            )
            content = response.choices[0].message.content or ""
            latency = int((time.perf_counter() - started) * 1000)
            return ConnectionTestResult(
                ok=bool(content.strip()),
                provider=config.provider,
                base_url=config.base_url,
                model_name=config.model_name,
                latency_ms=latency,
            )
        except Exception as exc:
            return ConnectionTestResult(
                ok=False,
                provider=config.provider,
                base_url=config.base_url,
                model_name=config.model_name,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
                suggestions=[
                    "Check base_url includes the OpenAI-compatible /v1 or provider-compatible path.",
                    "Check model_name is enabled for this API key.",
                    "Check the API key value; it is never logged in full.",
                    "Check network, firewall, or provider quota/rate limits.",
                ],
            )

    async def start_session(self, task_spec: TaskSpec, variables: dict, model_config: ModelConfig) -> SessionHandle:
        return SessionHandle(session_id=timestamped_id("sess"), task_id=task_spec.task_id, variables=variables)

    async def send_turn(self, session: SessionHandle, messages: list[dict], metadata: dict) -> ModelTurn:
        config = ModelConfig.model_validate(metadata["model_config"])
        client = self._client(config)
        started = time.perf_counter()
        response = await client.chat.completions.create(
            model=config.model_name,
            messages=messages,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout_seconds,
        )
        latency = int((time.perf_counter() - started) * 1000)
        message = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else {}
        return ModelTurn(
            content=message,
            finish_reason=response.choices[0].finish_reason,
            raw_output={
                "id": getattr(response, "id", None),
                "raw_response_hash": hashlib.sha1(str(raw).encode("utf-8")).hexdigest(),
            },
            latency_ms=latency,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        )

    async def close_session(self, session: SessionHandle) -> None:
        return None

    def _client(self, config: ModelConfig) -> AsyncOpenAI:
        return AsyncOpenAI(api_key=config.raw_api_key(), base_url=config.base_url, timeout=config.timeout_seconds)

