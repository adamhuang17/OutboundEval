from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from pydantic import BaseModel

from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.llm.structured_client import StructuredLLMClient, _try_repair_json


class _TinyModel(BaseModel):
    name: str


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, calls: list[dict]):
        self.calls = calls

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1 and "response_format" in kwargs:
            raise Exception(
                "Error code: 400 - {'error': {'code': 'InvalidParameter', "
                "'message': 'The parameter `response_format.type` specified in the request are not valid: "
                "`json_object` is not supported by this model.', 'param': 'response_format.type'}}"
            )
        return _FakeResponse('{"name": "ok"}')


class _FakeChat:
    def __init__(self, calls: list[dict]):
        self.completions = _FakeCompletions(calls)


class _FakeAsyncOpenAI:
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.chat = _FakeChat(self.calls)


class StructuredClientCompatibilityTest(unittest.TestCase):
    def test_invoke_json_falls_back_when_json_object_is_unsupported(self):
        _FakeAsyncOpenAI.calls = []
        config = ModelConfig(
            base_url="https://example.test/v1",
            api_key="secret-key",
            model_name="provider-model",
        )
        messages = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "ping"}]

        with patch("outbound_eval.llm.structured_client.AsyncOpenAI", _FakeAsyncOpenAI):
            result = asyncio.run(
                StructuredLLMClient().invoke_json(
                    model_config=config,
                    messages=messages,
                    output_model=_TinyModel,
                    max_retries=1,
                )
            )

        self.assertEqual(result.parsed.name, "ok")
        self.assertEqual(len(_FakeAsyncOpenAI.calls), 2)
        self.assertIn("response_format", _FakeAsyncOpenAI.calls[0])
        self.assertNotIn("response_format", _FakeAsyncOpenAI.calls[1])
        self.assertTrue(any("unsupported" in warning for warning in result.warnings))

    def test_repair_json_normalizes_smart_delimiter_quotes(self):
        raw = (
            "{\u201cname\u201d: \u201c\u5916\u547c\u201d, "
            "\u201clist\u201d: [\u201ca\u201d, \u201cb\u201d,],}"
        )

        parsed = json.loads(_try_repair_json(raw))

        self.assertEqual(parsed["name"], "\u5916\u547c")
        self.assertEqual(parsed["list"], ["a", "b"])

    def test_repair_json_keeps_smart_quotes_inside_valid_strings(self):
        raw = '{"text": "\u4ed6\u8bf4\u201c\u53ef\u4ee5\u201d"}'

        repaired = _try_repair_json(raw)
        parsed = json.loads(repaired)

        self.assertEqual(parsed["text"], "\u4ed6\u8bf4\u201c\u53ef\u4ee5\u201d")


if __name__ == "__main__":
    unittest.main()
