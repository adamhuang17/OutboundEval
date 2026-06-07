"""StructuredLLMClient — 统一的结构化 LLM 调用层。

所有 compiler / simulator / judge 必须通过此模块调用 LLM，禁止直接导入 openai.AsyncOpenAI。

特性：
- Pydantic schema-first：输出必须先定义 BaseModel，再调用
- JSON repair：仅修复格式，不补业务语义
- 最多 max_retries 次重试
- 写入 TraceEvent stub（可接后端 trace store）
"""
from __future__ import annotations

import json
import time
from typing import Any, Type, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from outbound_eval.domain.schemas_model import ModelConfig

T = TypeVar("T", bound=BaseModel)

_REPAIR_HINTS = [
    # 截断的 JSON 末尾补 }] 组合
    (r",\s*$", ""),
    (r",$", ""),
]


def _try_repair_json(text: str) -> str:
    """极简 JSON repair：去掉尾部多余逗号，补齐括号。不做语义修补。"""
    t = text.strip()
    # Remove markdown code fences
    if t.startswith("```"):
        lines = t.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        t = "\n".join(lines).strip()
    # Try to find JSON boundaries
    first_brace = t.find("{")
    first_bracket = t.find("[")
    if first_brace == -1 and first_bracket == -1:
        return t
    start = min(
        first_brace if first_brace != -1 else len(t),
        first_bracket if first_bracket != -1 else len(t),
    )
    t = t[start:]
    # Count unclosed braces / brackets
    open_braces = t.count("{") - t.count("}")
    open_brackets = t.count("[") - t.count("]")
    if open_braces > 0:
        t += "}" * open_braces
    if open_brackets > 0:
        t += "]" * open_brackets
    return t


class StructuredLLMResult(BaseModel):
    parsed: Any  # BaseModel instance
    raw_text: str
    repaired: bool = False
    retry_count: int = 0
    warnings: list[str] = []


class StructuredLLMClient:
    """统一 structured output 客户端。"""

    async def invoke_json(
        self,
        *,
        model_config: ModelConfig,
        messages: list[dict[str, str]],
        output_model: Type[T],
        stage: str = "unknown",
        temperature: float | None = None,
        max_retries: int = 3,
    ) -> StructuredLLMResult:
        temp = temperature if temperature is not None else model_config.temperature
        client = AsyncOpenAI(
            api_key=model_config.api_key,
            base_url=model_config.base_url,
        )
        warnings: list[str] = []
        repaired = False
        raw_text = ""

        for attempt in range(max_retries):
            try:
                started = time.perf_counter()
                resp = await client.chat.completions.create(
                    model=model_config.model_name,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=temp,
                    max_tokens=model_config.max_tokens,
                    timeout=model_config.timeout_seconds,
                    response_format={"type": "json_object"},
                )
                raw_text = resp.choices[0].message.content or ""
                try:
                    data = json.loads(raw_text)
                    parsed = output_model.model_validate(data)
                    return StructuredLLMResult(
                        parsed=parsed,
                        raw_text=raw_text,
                        repaired=repaired,
                        retry_count=attempt,
                        warnings=warnings,
                    )
                except (json.JSONDecodeError, ValidationError) as parse_err:
                    repaired_text = _try_repair_json(raw_text)
                    try:
                        data2 = json.loads(repaired_text)
                        parsed2 = output_model.model_validate(data2)
                        repaired = True
                        warnings.append(f"attempt {attempt}: JSON repaired ({type(parse_err).__name__})")
                        return StructuredLLMResult(
                            parsed=parsed2,
                            raw_text=raw_text,
                            repaired=True,
                            retry_count=attempt,
                            warnings=warnings,
                        )
                    except Exception as repair_err:
                        warnings.append(f"attempt {attempt}: repair failed ({repair_err})")
                        if attempt == max_retries - 1:
                            raise ValueError(
                                f"LLM output could not be parsed into {output_model.__name__} after {max_retries} attempts. "
                                f"Last error: {repair_err}. Raw: {raw_text[:300]}"
                            ) from repair_err
            except Exception as exc:
                warnings.append(f"attempt {attempt}: LLM call failed ({exc})")
                if attempt == max_retries - 1:
                    raise

        raise RuntimeError("StructuredLLMClient: exhausted retries")

    async def invoke_text(
        self,
        *,
        model_config: ModelConfig,
        messages: list[dict[str, str]],
        temperature: float | None = None,
    ) -> str:
        temp = temperature if temperature is not None else model_config.temperature
        client = AsyncOpenAI(
            api_key=model_config.api_key,
            base_url=model_config.base_url,
        )
        resp = await client.chat.completions.create(
            model=model_config.model_name,
            messages=messages,  # type: ignore[arg-type]
            temperature=temp,
            max_tokens=model_config.max_tokens,
            timeout=model_config.timeout_seconds,
        )
        return resp.choices[0].message.content or ""


_default_client = StructuredLLMClient()


def get_client() -> StructuredLLMClient:
    return _default_client
