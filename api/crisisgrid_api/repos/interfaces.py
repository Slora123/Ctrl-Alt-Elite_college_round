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

    def list(self, run_id: str, *, after_id: str | None = None, limit: int = 1000) -> list[RunEvent]: ...
