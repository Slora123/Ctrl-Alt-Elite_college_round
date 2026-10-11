from __future__ import annotations

from .interfaces import RunEvent


class InMemoryEventStore:
    """Test/dev double for EventStore."""

    def __init__(self) -> None:
        self._events: list[RunEvent] = []

    def append(self, event: RunEvent) -> RunEvent:
        self._events.append(event)
        return event

    def delete_runs(self, run_ids: list[str]) -> None:
        gone = set(run_ids)
        self._events = [e for e in self._events if e.run_id not in gone]

    def list(self, run_id: str, *, after_id: str | None = None, limit: int = 1000) -> list[RunEvent]:
        evs = [e for e in self._events if e.run_id == run_id]
        if after_id is not None:
            evs = [e for e in evs if e.id > after_id]
        return evs[:limit]
