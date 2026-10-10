"""Typed, persisted, subscribable event bus.

Persist first, then fan out. Payloads are scrubbed before either happens, so
secrets can reach neither the DB nor subscribers.

SQLite allows a single writer: never call ``publish`` while holding an open
UnitOfWork on the same thread. If an event must commit atomically with other
writes, use ``append(uow, ...)`` inside the unit of work and ``dispatch(event)``
after it commits.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Iterable

from ..domain import Event, EventType
from ..repos import UnitOfWork, UnitOfWorkFactory
from ..security import Scrubber

log = logging.getLogger(__name__)
Callback = Callable[[Event], None]


class Subscription:
    def __init__(self, bus: "EventBus", callback: Callback, types, run_id, paper_env_id):
        self._bus, self.callback = bus, callback
        self.types = set(types) if types else None
        self.run_id, self.paper_env_id = run_id, paper_env_id

    def matches(self, e: Event) -> bool:
        return ((self.types is None or e.type in self.types)
                and (self.run_id is None or e.run_id == self.run_id)
                and (self.paper_env_id is None or e.paper_env_id == self.paper_env_id))

    def close(self) -> None:
        self._bus._remove(self)


class EventBus:
    def __init__(self, uow_factory: UnitOfWorkFactory, scrubber: Scrubber | None = None):
        self._uow = uow_factory
        self.scrubber = scrubber or Scrubber()
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()

    # ---- publishing ----
    def make(self, type: EventType, payload: dict[str, Any] | None = None, *,
             paper_env_id: str | None = None, run_id: str | None = None) -> Event:
        return Event(type=type, paper_env_id=paper_env_id, run_id=run_id,
                     payload=self.scrubber.scrub_obj(payload or {}))

    def append(self, uow: UnitOfWork, type: EventType, payload: dict[str, Any] | None = None, **kw) -> Event:
        """Persist inside the caller's unit of work. Call ``dispatch`` after commit."""
        return uow.events.append(self.make(type, payload, **kw))

    def publish(self, type: EventType, payload: dict[str, Any] | None = None, **kw) -> Event:
        with self._uow() as u:
            event = u.events.append(self.make(type, payload, **kw))
        self.dispatch(event)
        return event

    def dispatch(self, event: Event) -> None:
        with self._lock:
            subs = [s for s in self._subs if s.matches(event)]
        for s in subs:
            try:
                s.callback(event)
            except Exception:  # a bad subscriber must never break the producer
                log.exception("event subscriber failed")

    # ---- subscribing ----
    def subscribe(self, callback: Callback, types: Iterable[EventType] | None = None, *,
                  run_id: str | None = None, paper_env_id: str | None = None) -> Subscription:
        sub = Subscription(self, callback, types, run_id, paper_env_id)
        with self._lock:
            self._subs.append(sub)
        return sub

    def subscribe_queue(self, **kw) -> tuple["queue.Queue[Event]", Subscription]:
        q: "queue.Queue[Event]" = queue.Queue()
        return q, self.subscribe(q.put, **kw)

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    # ---- history ----
    def history(self, after_seq: int = 0, limit: int = 1000, **filters) -> list[Event]:
        with self._uow() as u:
            return u.events.list(after_seq=after_seq, limit=limit, **filters)
