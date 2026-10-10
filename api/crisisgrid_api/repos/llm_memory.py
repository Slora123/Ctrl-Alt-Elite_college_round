from __future__ import annotations

from ..gateway.types import LLMCallRecord, LLMResponse


class InMemoryCache:
    def __init__(self) -> None:
        self._d: dict[str, LLMResponse] = {}

    def get(self, key: str) -> LLMResponse | None:
        return self._d.get(key)

    def set(self, key: str, value: LLMResponse) -> None:
        self._d[key] = value


class InMemoryAuditLog:
    def __init__(self) -> None:
        self.records: list[LLMCallRecord] = []

    def append(self, record: LLMCallRecord) -> None:
        self.records.append(record)

    def list(self, *, run_id: str | None = None, limit: int = 1000) -> list[LLMCallRecord]:
        return [r for r in self.records if run_id is None or r.run_id == run_id][:limit]
