"""Job model, state machine, capability matching and retry policy (system_design 7.1). Pure logic; the queue that stores
jobs lives in the API package.

Rules from the design:
  - status: queued -> assigned -> running -> succeeded | failed | timeout | cancelled | infra_failed
  - retry ONLY on infrastructure failure (worker vanished, kernel death/OOM, network), never on user-code failure,
    and only a bounded number of times;
  - a job whose worker stops heartbeating is reaped (treated as an infrastructure failure).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from .ids import new_id
from .models import as_utc

__all__ = [
    "JobStatus", "JobMode", "Capabilities", "JobRequirements", "JobSpec", "TERMINAL", "can_transition", "match",
    "failure_outcome", "is_stale", "InvalidTransition",
]

JobStatus = Literal["queued", "assigned", "running", "succeeded", "failed", "timeout", "cancelled", "infra_failed"]
JobMode = Literal["batch", "clean_notebook"]
TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "timeout", "cancelled", "infra_failed"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"assigned", "cancelled"}),
    "assigned": frozenset({"running", "queued", "cancelled", "infra_failed"}),  # back to queued: worker never started
    "running": frozenset({"succeeded", "failed", "timeout", "cancelled", "infra_failed"}),
}


class InvalidTransition(ValueError):
    pass


def can_transition(a: str, b: str) -> bool:
    return b in _TRANSITIONS.get(a, frozenset())


class Capabilities(BaseModel):
    """What a worker advertises when it registers."""

    gpu: bool = False
    network: bool = True
    max_runtime_s: int = 3600
    isolation: Literal["docker", "subprocess", "remote_vm"] = "subprocess"  # 'subprocess' = lower isolation (no Docker); 'remote_vm' = rented cloud VM (Colab)
    python_versions: list[str] = Field(default_factory=lambda: ["3.12"])
    cost_per_hr: float = 0.0


class JobRequirements(BaseModel):
    gpu_required: bool = False
    needs_network: bool = False  # e.g. dataset not cached yet
    est_runtime_s: int = 600
    python: str | None = None  # None: run with whatever interpreter the worker has; set it only when a job truly needs one
    require_strong_isolation: bool = False  # untrusted code on shared compute: refuse "lower isolation" workers


class JobSpec(BaseModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    graph_id: str
    mode: JobMode = "clean_notebook"
    notebook_version: int | None = None
    seed: int = 0
    requirements: JobRequirements = Field(default_factory=JobRequirements)
    payload: dict[str, Any] = Field(default_factory=dict)  # cells, dataset ids, env spec: everything a worker needs
    timeout_s: int = 1800
    max_infra_retries: int = 1  # "requeue once on infra failure"


def match(req: JobRequirements, caps: Capabilities) -> tuple[bool, list[str]]:
    """Can this worker run this job? Returns (ok, reasons it cannot)."""
    why: list[str] = []
    if req.gpu_required and not caps.gpu:
        why.append("job needs a GPU")
    if req.needs_network and not caps.network:
        why.append("job needs network access")
    if req.est_runtime_s > caps.max_runtime_s:
        why.append(f"estimated runtime {req.est_runtime_s}s exceeds the worker's limit {caps.max_runtime_s}s")
    if req.python is not None and req.python not in caps.python_versions:
        why.append(f"python {req.python} not available")
    if req.require_strong_isolation and caps.isolation != "docker":
        why.append("job requires strong isolation; this worker is lower-isolation (no Docker)")
    return (not why, why)


def failure_outcome(status: str, attempt: int, max_infra_retries: int) -> Literal["requeue", "final"]:
    """After a job ended in `status` on its `attempt`-th try (0-based): retry only infrastructure failures, and only up
    to the bound. A user-code failure or a timeout is a result, not something to hide by retrying."""
    if status == "infra_failed" and attempt < max_infra_retries:
        return "requeue"
    return "final"


def is_stale(last_heartbeat: datetime, now: datetime, timeout: timedelta) -> bool:
    return as_utc(now) - as_utc(last_heartbeat) > timeout
