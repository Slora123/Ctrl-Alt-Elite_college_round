"""Repository interfaces. Services depend on these, never on the ORM (only `db/` imports SQLAlchemy)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from crisisgrid_core.ids import new_id
from crisisgrid_core.models import utcnow


class RunEvent(BaseModel):
    """Event log entry: {run_id, ts, module, kind, payload}."""

    id: str = Field(default_factory=new_id)
    run_id: str
    ts: datetime = Field(default_factory=utcnow)
    module: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EventStore(Protocol):
    def append(self, event: RunEvent) -> RunEvent: ...

    def delete_runs(self, run_ids: list[str]) -> None: ...

    def list(self, run_id: str, *, after_id: str | None = None, limit: int = 1000) -> list[RunEvent]: ...


class PaperRepo(Protocol):
    def add(self, record: Any, pdf: bytes) -> None: ...

    def get(self, paper_id: str) -> Any | None: ...

    def get_pdf(self, paper_id: str) -> bytes | None: ...

    def delete(self, paper_id: str) -> None: ...

    def list(self) -> list[Any]: ...


class RunRepo(Protocol):
    def save(self, state: Any) -> None: ...

    def get(self, run_id: str) -> Any | None: ...

    def delete_for_paper(self, paper_id: str) -> list[str]: ...

    def list(self, paper_id: str | None = None) -> list[Any]: ...


class GateRepo(Protocol):
    def create(self, gate: Any) -> None: ...

    def get(self, gate_id: str) -> Any | None: ...

    def update(self, gate: Any) -> None: ...

    def delete_for_runs(self, run_ids: list[str]) -> None: ...

    def list(self, *, run_id: str | None = None, status: str | None = None) -> list[Any]: ...
