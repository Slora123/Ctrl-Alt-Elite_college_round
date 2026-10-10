"""Pre-execute hook seam for the future agent harness / guardrails.

``PreExecuteHook.check(action)`` is called before EVERY cell execution, package
install, dataset fetch and env rebuild. Anything other than ``allow`` pauses the
action cleanly: a ``BlockedAction`` row is stored, an ``action_blocked`` event is
emitted and nothing runs. ``ActionGate.resume_blocked(action_id, decision)``
continues (allow) or finalises (deny) it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .domain import ActionKind, BlockedAction, BlockedActionStatus, DecisionKind, EventType
from .events import EventBus
from .repos import UnitOfWorkFactory


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    paper_env_id: str | None = None
    run_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str = ""

    @staticmethod
    def allow(reason: str = "") -> "Decision":
        return Decision(DecisionKind.ALLOW, reason)

    @staticmethod
    def deny(reason: str) -> "Decision":
        return Decision(DecisionKind.DENY, reason)

    @staticmethod
    def needs_approval(reason: str) -> "Decision":
        return Decision(DecisionKind.NEEDS_APPROVAL, reason)


class PreExecuteHook(Protocol):
    def check(self, action: Action) -> Decision: ...


class AllowAllHook:
    def check(self, action: Action) -> Decision:
        return Decision.allow("default hook")


class ActionBlocked(Exception):
    def __init__(self, action_id: str, decision: Decision, action: Action):
        super().__init__(f"{action.kind.value} blocked ({decision.kind.value}): {decision.reason}")
        self.action_id, self.decision, self.action = action_id, decision, action


Executor = Callable[[BlockedAction], Any]


class ActionGate:
    def __init__(self, hook: PreExecuteHook, uow_factory: UnitOfWorkFactory, bus: EventBus):
        self.hook, self._uow, self.bus = hook, uow_factory, bus
        self._executors: dict[ActionKind, Executor] = {}

    def register_executor(self, kind: ActionKind, fn: Executor) -> None:
        """Services register how to carry out an approved action of ``kind``."""
        self._executors[kind] = fn

    def check(self, action: Action) -> None:
        """Raise ``ActionBlocked`` (after persisting it) unless the hook allows."""
        decision = self.hook.check(action)
        if decision.kind == DecisionKind.ALLOW:
            return
        blocked = BlockedAction(
            kind=action.kind, paper_env_id=action.paper_env_id, run_id=action.run_id,
            payload=self.bus.scrubber.scrub_obj(action.payload),
            decision=decision.kind.value, reason=self.bus.scrubber.scrub(decision.reason),
        )
        with self._uow() as u:
            u.blocked.add(blocked)
            ev = self.bus.append(u, EventType.ACTION_BLOCKED, {
                "action_id": blocked.id, "kind": action.kind.value, "decision": decision.kind.value,
                "reason": decision.reason, "payload": action.payload,
            }, paper_env_id=action.paper_env_id, run_id=action.run_id)
        self.bus.dispatch(ev)
        raise ActionBlocked(blocked.id, decision, action)

    def resume_blocked(self, action_id: str, decision: Decision) -> Any:
        """Apply a (human/harness) decision to a paused action.

        allow  -> run the registered executor; return its result.
        other  -> mark the action denied; nothing runs; returns None.
        The hook is not consulted again: this decision *is* the approval.
        """
        with self._uow() as u:
            blocked = u.blocked.get(action_id)
            if blocked.status != BlockedActionStatus.PENDING:
                raise ValueError(f"action {action_id} already resolved ({blocked.status.value})")
            allowed = decision.kind == DecisionKind.ALLOW
            blocked.status = BlockedActionStatus.ALLOWED if allowed else BlockedActionStatus.DENIED
            blocked.resolved_at = datetime.now(timezone.utc)
            u.blocked.update(blocked)
            ev = self.bus.append(u, EventType.ACTION_RESOLVED, {
                "action_id": action_id, "decision": decision.kind.value, "reason": decision.reason,
            }, paper_env_id=blocked.paper_env_id, run_id=blocked.run_id)
        self.bus.dispatch(ev)
        if not allowed:
            return None
        executor = self._executors.get(blocked.kind)
        if executor is None:
            raise LookupError(f"no executor registered for {blocked.kind.value}")
        return executor(blocked)
