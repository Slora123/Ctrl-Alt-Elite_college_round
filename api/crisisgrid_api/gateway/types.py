from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from crisisgrid_core.ids import new_id
from crisisgrid_core.models import utcnow


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(BaseModel):
    text: str
    provider: str = ""
    model: str = ""  # exact model version reported by the provider, recorded for reproducibility
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


ErrorKind = Literal["rate_limit", "timeout", "server", "auth", "invalid"]


class ProviderError(Exception):
    """Raised by a ModelClient. rate_limit -> cool the provider down and fail over; timeout/server -> back off and
    retry; auth/invalid -> not retryable on this provider."""

    def __init__(self, kind: ErrorKind, message: str = "", retry_after: float | None = None) -> None:
        self.kind = kind
        self.retry_after = retry_after
        super().__init__(f"{kind}: {message}")


class AllProvidersFailed(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("all providers failed: " + "; ".join(errors))


class ModelClient(Protocol):
    def complete(self, model: str, messages: list[Message], *, temperature: float, max_tokens: int) -> LLMResponse: ...


class LLMCallRecord(BaseModel):
    """Audit record: CrisisGrid's own analysis must be reproducible, so every call stores prompt, output, model."""

    id: str = Field(default_factory=new_id)
    ts: datetime = Field(default_factory=utcnow)
    run_id: str | None = None
    agent: str
    provider: str
    model: str
    messages: list[Message]
    response: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    error: str | None = None


class LLMAuditLog(Protocol):
    def append(self, record: LLMCallRecord) -> None: ...

    def list(self, *, run_id: str | None = None, limit: int = 1000) -> list[LLMCallRecord]: ...


class Cache(Protocol):
    def get(self, key: str) -> LLMResponse | None: ...

    def set(self, key: str, value: LLMResponse) -> None: ...
