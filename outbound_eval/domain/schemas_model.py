from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai_compatible"
    base_url: str
    api_key: str
    model_name: str
    temperature: float = 0.2
    max_tokens: int = 512
    timeout_seconds: int = 30
    connection_tested: bool = False

    @field_serializer("api_key")
    def redact_api_key(self, value: str) -> str:
        if len(value) <= 8:
            return "***"
        return f"{value[:3]}***{value[-3:]}"

    def raw_api_key(self) -> str:
        return self.api_key

    def redacted(self) -> dict:
        data = self.model_dump()
        data["api_key"] = self.__pydantic_serializer__.to_python(self)["api_key"]
        return data


class ConnectionTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    provider: str
    base_url: str
    model_name: str
    latency_ms: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    suggestions: list[str] = Field(default_factory=list)


class SessionHandle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    task_id: str
    variables: dict = Field(default_factory=dict)
