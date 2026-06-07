"""Structured LLM client with plain-JSON fallback and stage fail-fast controls."""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Literal, Type, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from outbound_eval.domain.schemas_model import ModelConfig

T = TypeVar("T", bound=BaseModel)

ResponseFormatMode = Literal["auto", "force", "avoid"]

_JSON_MODE_HINT = (
    'Return only one valid JSON object. Use standard ASCII double quotes (") '
    "for every JSON key and string delimiter. Do not use Chinese/full-width "
    "quotation marks, Markdown code fences, comments, or trailing commas."
)

_SMART_QUOTES = {"\u201c", "\u201d", "\u201e", "\u201f", "\uff02"}
_STRING_START_PREV_CHARS = {"", "{", "[", ":", ","}
_STRING_END_NEXT_CHARS = {"", ":", ",", "}", "]"}


def _prev_nonspace(text: str, index: int) -> str:
    for i in range(index, -1, -1):
        if not text[i].isspace():
            return text[i]
    return ""


def _next_nonspace(text: str, index: int) -> str:
    for i in range(index, len(text)):
        if not text[i].isspace():
            return text[i]
    return ""


def _normalize_smart_json_quotes(text: str) -> str:
    out: list[str] = []
    in_string = False
    string_delim = ""
    escape = False

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if string_delim == '"' and ch == '"':
                out.append(ch)
                in_string = False
                string_delim = ""
                continue
            if string_delim == "smart" and ch in _SMART_QUOTES:
                if _next_nonspace(text, i + 1) in _STRING_END_NEXT_CHARS:
                    out.append('"')
                    in_string = False
                    string_delim = ""
                else:
                    out.append(ch)
                continue
            if string_delim == "smart" and ch == '"':
                out.append('\\"')
                continue
            out.append(ch)
            continue

        if ch == '"':
            out.append(ch)
            in_string = True
            string_delim = '"'
            continue
        if ch in _SMART_QUOTES and _prev_nonspace(text, i - 1) in _STRING_START_PREV_CHARS:
            out.append('"')
            in_string = True
            string_delim = "smart"
            continue
        out.append(ch)

    return "".join(out)


def _messages_with_json_hint(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    if not messages:
        return [{"role": "system", "content": _JSON_MODE_HINT}]

    hinted = [dict(message) for message in messages]
    for message in hinted:
        if message.get("role") == "system":
            content = message.get("content", "")
            if _JSON_MODE_HINT not in content:
                message["content"] = f"{content}\n\n{_JSON_MODE_HINT}".strip()
            return hinted
    return [{"role": "system", "content": _JSON_MODE_HINT}, *hinted]


def _response_format_is_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "response_format" in message
        and "json_object" in message
        and any(
            marker in message
            for marker in ("not supported", "not valid", "invalidparameter", "invalid parameter", "unsupported")
        )
    )


def _try_repair_json(text: str) -> str:
    """Repair formatting-only JSON issues without adding domain content."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        t = "\n".join(lines).strip()

    first_brace = t.find("{")
    first_bracket = t.find("[")
    if first_brace == -1 and first_bracket == -1:
        return t
    start = min(
        first_brace if first_brace != -1 else len(t),
        first_bracket if first_bracket != -1 else len(t),
    )
    t = t[start:]
    open_braces = t.count("{") - t.count("}")
    open_brackets = t.count("[") - t.count("]")
    if open_braces > 0:
        t += "}" * open_braces
    if open_brackets > 0:
        t += "]" * open_brackets
    t = _normalize_smart_json_quotes(t)
    return re.sub(r",\s*([}\]])", r"\1", t)


class StructuredLLMResult(BaseModel):
    parsed: Any
    raw_text: str
    repaired: bool = False
    retry_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    stage: str = "unknown"
    model_name: str = ""
    duration_ms: int | None = None


class ModelCapabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    base_url: str
    model_name: str
    short_text_ok: bool = False
    json_object_supported: bool = False
    plain_json_supported: bool = False
    long_prompt_latency_ms: int | None = None
    schema_compile_smoke: bool = False
    max_stable_prompt_chars: int = 0
    recommended_mode: Literal[
        "staged_response_format",
        "staged_plain_json",
        "short_only",
        "unavailable",
    ] = "unavailable"
    response_format: Literal["auto", "avoid", "force"] = "auto"
    compile_mode: Literal["staged_response_format", "staged_plain_json", "short_only"] = "short_only"
    max_stage_timeout: int = 35
    max_retries: int = 1
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class _CircuitState(BaseModel):
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""


class _CapabilityTiny(BaseModel):
    ok: bool = True
    name: str = "ok"


def model_runtime_profile(config: ModelConfig) -> ModelCapabilityProfile:
    """Static hints for known OpenAI-compatible providers/models."""
    name = config.model_name.lower()
    base = config.base_url.lower()
    response_format: Literal["auto", "avoid", "force"] = "auto"
    compile_mode: Literal["staged_response_format", "staged_plain_json", "short_only"] = "staged_response_format"
    max_stage_timeout = min(max(int(config.timeout_seconds or 35), 15), 60)
    max_retries = 1
    warnings: list[str] = []

    if "deepseek" in name or "ark" in base or "volc" in base:
        response_format = "avoid"
        compile_mode = "staged_plain_json"
        max_stage_timeout = min(max_stage_timeout, 35)
        warnings.append("Static profile prefers plain JSON for this provider/model family.")

    if "v4-pro" in name or "deepseek-v4-pro" in name:
        response_format = "avoid"
        compile_mode = "staged_plain_json"
        max_stage_timeout = min(max_stage_timeout, 35)
        max_retries = 1
        warnings.append("deepseek-v4-pro is treated as staged_plain_json/response_format=avoid.")

    return ModelCapabilityProfile(
        provider=config.provider,
        base_url=config.base_url,
        model_name=config.model_name,
        response_format=response_format,
        compile_mode=compile_mode,
        max_stage_timeout=max_stage_timeout,
        max_retries=max_retries,
        recommended_mode=compile_mode,
        warnings=warnings,
    )


class StructuredLLMClient:
    """Unified structured-output client for compiler, simulator, and judge calls."""

    _circuit: dict[tuple[str, str, str], _CircuitState] = {}
    circuit_failure_threshold: int = 2
    circuit_cooldown_seconds: int = 90

    async def invoke_json(
        self,
        *,
        model_config: ModelConfig,
        messages: list[dict[str, str]],
        output_model: Type[T],
        stage: str = "unknown",
        temperature: float | None = None,
        max_retries: int = 3,
        stage_timeout: float | None = None,
        response_format: ResponseFormatMode = "auto",
        fallback_configs: list[ModelConfig] | None = None,
    ) -> StructuredLLMResult:
        chain = [model_config, *(fallback_configs or [])]
        errors: list[str] = []
        for idx, config in enumerate(chain):
            try:
                result = await self._invoke_json_one(
                    model_config=config,
                    messages=messages,
                    output_model=output_model,
                    stage=stage,
                    temperature=temperature,
                    max_retries=max(1, max_retries),
                    stage_timeout=stage_timeout,
                    response_format=response_format,
                )
                if idx > 0:
                    result.warnings.append(f"fallback model used after {idx} prior failure(s)")
                return result
            except Exception as exc:
                errors.append(f"{config.model_name}: {exc}")
                continue
        raise RuntimeError(f"StructuredLLMClient fallback chain exhausted for stage={stage}: {' | '.join(errors)}")

    async def _invoke_json_one(
        self,
        *,
        model_config: ModelConfig,
        messages: list[dict[str, str]],
        output_model: Type[T],
        stage: str,
        temperature: float | None,
        max_retries: int,
        stage_timeout: float | None,
        response_format: ResponseFormatMode,
    ) -> StructuredLLMResult:
        profile = model_runtime_profile(model_config)
        effective_timeout = float(stage_timeout or profile.max_stage_timeout or model_config.timeout_seconds or 35)
        self._check_circuit(model_config, stage)

        temp = temperature if temperature is not None else model_config.temperature
        client = AsyncOpenAI(
            api_key=model_config.raw_api_key(),
            base_url=model_config.base_url,
            timeout=effective_timeout,
        )
        warnings: list[str] = list(profile.warnings)
        repaired = False
        raw_text = ""
        use_response_format = self._should_use_response_format(response_format, profile)
        request_messages = _messages_with_json_hint(messages)
        started = time.perf_counter()

        try:
            for attempt in range(max_retries):
                try:
                    request: dict[str, Any] = {
                        "model": model_config.model_name,
                        "messages": request_messages,
                        "temperature": temp,
                        "max_tokens": model_config.max_tokens,
                        "timeout": effective_timeout,
                    }
                    if use_response_format:
                        request["response_format"] = {"type": "json_object"}

                    try:
                        resp = await self._create_with_timeout(client, request, effective_timeout)
                    except Exception as exc:
                        can_plain_retry = (
                            response_format == "auto"
                            and use_response_format
                            and _response_format_is_unsupported(exc)
                        )
                        if not can_plain_retry:
                            raise
                        use_response_format = False
                        warnings.append(
                            f"attempt {attempt}: response_format json_object unsupported; retried with plain JSON"
                        )
                        request.pop("response_format", None)
                        resp = await self._create_with_timeout(client, request, effective_timeout)

                    raw_text = resp.choices[0].message.content or ""
                    try:
                        data = json.loads(raw_text)
                        parsed = output_model.model_validate(data)
                        self._record_success(model_config, stage)
                        return StructuredLLMResult(
                            parsed=parsed,
                            raw_text=raw_text,
                            repaired=repaired,
                            retry_count=attempt,
                            warnings=warnings,
                            stage=stage,
                            model_name=model_config.model_name,
                            duration_ms=int((time.perf_counter() - started) * 1000),
                        )
                    except (json.JSONDecodeError, ValidationError) as parse_err:
                        repaired_text = _try_repair_json(raw_text)
                        try:
                            data2 = json.loads(repaired_text)
                            parsed2 = output_model.model_validate(data2)
                            self._record_success(model_config, stage)
                            warnings.append(f"attempt {attempt}: JSON repaired ({type(parse_err).__name__})")
                            return StructuredLLMResult(
                                parsed=parsed2,
                                raw_text=raw_text,
                                repaired=True,
                                retry_count=attempt,
                                warnings=warnings,
                                stage=stage,
                                model_name=model_config.model_name,
                                duration_ms=int((time.perf_counter() - started) * 1000),
                            )
                        except Exception as repair_err:
                            warnings.append(f"attempt {attempt}: repair failed ({repair_err})")
                            if attempt == max_retries - 1:
                                raise ValueError(
                                    f"LLM output could not be parsed into {output_model.__name__}. "
                                    f"Last error: {repair_err}. Raw: {raw_text[:300]}"
                                ) from repair_err
                except Exception as exc:
                    warnings.append(f"attempt {attempt}: LLM call failed ({exc})")
                    if attempt == max_retries - 1:
                        raise
        except Exception as exc:
            self._record_failure(model_config, stage, exc)
            raise

        raise RuntimeError("StructuredLLMClient: exhausted retries")

    async def invoke_text(
        self,
        *,
        model_config: ModelConfig,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        stage: str = "text",
        stage_timeout: float | None = None,
    ) -> str:
        profile = model_runtime_profile(model_config)
        effective_timeout = float(stage_timeout or profile.max_stage_timeout or model_config.timeout_seconds or 35)
        self._check_circuit(model_config, stage)
        temp = temperature if temperature is not None else model_config.temperature
        client = AsyncOpenAI(
            api_key=model_config.raw_api_key(),
            base_url=model_config.base_url,
            timeout=effective_timeout,
        )
        request = {
            "model": model_config.model_name,
            "messages": messages,
            "temperature": temp,
            "max_tokens": model_config.max_tokens,
            "timeout": effective_timeout,
        }
        try:
            resp = await self._create_with_timeout(client, request, effective_timeout)
            self._record_success(model_config, stage)
            return resp.choices[0].message.content or ""
        except Exception as exc:
            self._record_failure(model_config, stage, exc)
            raise

    async def probe_capability(self, model_config: ModelConfig) -> ModelCapabilityProfile:
        """Probe whether a model can handle compile-shaped staged JSON calls."""
        profile = model_runtime_profile(model_config)
        short_timeout = min(profile.max_stage_timeout, 10)
        long_timeout = min(profile.max_stage_timeout, 20)

        try:
            content = await self.invoke_text(
                model_config=model_config,
                messages=[{"role": "user", "content": "Return OK."}],
                temperature=0,
                stage="capability.short_text",
                stage_timeout=short_timeout,
            )
            profile.short_text_ok = bool(content.strip())
        except Exception as exc:
            profile.errors.append(f"short_text: {exc}")

        json_messages = [
            {"role": "system", "content": _JSON_MODE_HINT},
            {"role": "user", "content": 'Return {"ok": true, "name": "probe"} as JSON.'},
        ]
        try:
            await self.invoke_json(
                model_config=model_config,
                messages=json_messages,
                output_model=_CapabilityTiny,
                stage="capability.json_object",
                temperature=0,
                max_retries=1,
                stage_timeout=short_timeout,
                response_format="force",
            )
            profile.json_object_supported = True
        except Exception as exc:
            profile.errors.append(f"json_object: {exc}")

        long_body = "\n".join(
            f"- node_{i:04d}: caller must follow step {i}, cite source, and return plain JSON."
            for i in range(120)
        )
        try:
            started = time.perf_counter()
            await self.invoke_json(
                model_config=model_config,
                messages=[
                    {"role": "system", "content": _JSON_MODE_HINT},
                    {"role": "user", "content": f"Summarize this staged compiler probe as JSON.\n{long_body}"},
                ],
                output_model=_CapabilityTiny,
                stage="capability.plain_json_long",
                temperature=0,
                max_retries=1,
                stage_timeout=long_timeout,
                response_format="avoid",
            )
            profile.plain_json_supported = True
            profile.schema_compile_smoke = True
            profile.long_prompt_latency_ms = int((time.perf_counter() - started) * 1000)
        except Exception as exc:
            profile.errors.append(f"plain_json_long: {exc}")

        if profile.json_object_supported and profile.schema_compile_smoke:
            profile.recommended_mode = "staged_response_format"
            profile.compile_mode = "staged_response_format"
            profile.response_format = "auto"
            profile.max_stable_prompt_chars = 12000
        elif profile.plain_json_supported:
            profile.recommended_mode = "staged_plain_json"
            profile.compile_mode = "staged_plain_json"
            profile.response_format = "avoid"
            profile.max_stable_prompt_chars = 8000
        elif profile.short_text_ok:
            profile.recommended_mode = "short_only"
            profile.compile_mode = "short_only"
            profile.response_format = "avoid"
            profile.max_stable_prompt_chars = 1500
        else:
            profile.recommended_mode = "unavailable"
            profile.max_stable_prompt_chars = 0
        return profile

    async def _create_with_timeout(self, client: AsyncOpenAI, request: dict[str, Any], timeout: float):
        return await asyncio.wait_for(client.chat.completions.create(**request), timeout=timeout)

    def _should_use_response_format(self, mode: ResponseFormatMode, profile: ModelCapabilityProfile) -> bool:
        if mode == "force":
            return True
        if mode == "avoid":
            return False
        return profile.response_format != "avoid"

    def _circuit_key(self, config: ModelConfig, stage: str) -> tuple[str, str, str]:
        return (config.base_url, config.model_name, stage)

    def _check_circuit(self, config: ModelConfig, stage: str) -> None:
        state = self._circuit.get(self._circuit_key(config, stage))
        if not state:
            return
        now = time.time()
        if state.opened_until > now:
            remain = int(state.opened_until - now)
            raise RuntimeError(
                f"circuit breaker open for model={config.model_name} stage={stage}; "
                f"cooldown_remaining={remain}s; last_error={state.last_error}"
            )

    def _record_success(self, config: ModelConfig, stage: str) -> None:
        self._circuit.pop(self._circuit_key(config, stage), None)

    def _record_failure(self, config: ModelConfig, stage: str, exc: Exception) -> None:
        key = self._circuit_key(config, stage)
        state = self._circuit.get(key, _CircuitState())
        state.failures += 1
        state.last_error = str(exc)[:300]
        if state.failures >= self.circuit_failure_threshold:
            state.opened_until = time.time() + self.circuit_cooldown_seconds
        self._circuit[key] = state


_default_client = StructuredLLMClient()


def get_client() -> StructuredLLMClient:
    return _default_client
