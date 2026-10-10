from __future__ import annotations

from sqlalchemy import select

from ..repos.interfaces import RunEvent
from .base import SqlRepository
from .tables import RunEventRow


class SqlEventStore(SqlRepository):
    """Append-only event log (EventStore)."""

    def append(self, event: RunEvent) -> RunEvent:
        with self._scope() as s:
            s.add(RunEventRow(**event.model_dump()))
        return event

    def list(self, run_id: str, *, after_id: str | None = None, limit: int = 1000) -> list[RunEvent]:
        q = select(RunEventRow).where(RunEventRow.run_id == run_id)
        if after_id is not None:
            q = q.where(RunEventRow.id > after_id)
        with self._scope() as s:
            rows = s.scalars(q.order_by(RunEventRow.id).limit(limit)).all()
            return [
                RunEvent(id=r.id, run_id=r.run_id, ts=r.ts, module=r.module, kind=r.kind, payload=r.payload)
                for r in rows
            ]
