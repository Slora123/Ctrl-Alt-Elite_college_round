"""HITL gate manager: a gate pauses a run until a human answers. The gate is a database row, so a restart mid-wait loses
nothing."""
from __future__ import annotations

from typing import Any

from crisisgrid_core.models import utcnow

from ..repos.interfaces import EventStore, GateRepo, RunEvent
from .models import ALLOWED_ACTIONS, Gate, GateKind


class GateError(ValueError):
    pass


class GateAlreadyResolved(GateError):
    pass


class GateManager:
    def __init__(self, gates: GateRepo, events: EventStore) -> None:
        self.gates, self.events = gates, events

    def open(self, run_id: str, kind: GateKind, context: dict[str, Any], *, exclude: list[str] | None = None) -> Gate:
        actions = [a for a in ALLOWED_ACTIONS[kind] if a not in (exclude or [])]
        gate = Gate(run_id=run_id, kind=kind, context=context, allowed_actions=actions)
        self.gates.create(gate)
        self.events.append(RunEvent(run_id=run_id, module="gates", kind="gate_opened",
                                    payload={"gate_id": gate.id, "gate_kind": kind, "allowed_actions": gate.allowed_actions}))
        return gate

    def respond(self, gate_id: str, action: str, payload: dict[str, Any] | None = None) -> Gate:
        gate = self.gates.get(gate_id)
        if gate is None:
            raise KeyError(gate_id)
        if gate.status != "open":
            raise GateAlreadyResolved(f"gate {gate_id} is already resolved")
        if action not in gate.allowed_actions:
            raise GateError(f"action '{action}' is not allowed for a '{gate.kind}' gate (allowed: {', '.join(gate.allowed_actions)})")
        gate = gate.model_copy(update={"status": "resolved", "action": action, "response": payload or {}, "resolved_at": utcnow()})
        self.gates.update(gate)
        self.events.append(RunEvent(run_id=gate.run_id, module="gates", kind="gate_resolved",
                                    payload={"gate_id": gate.id, "gate_kind": gate.kind, "action": action}))
        return gate
