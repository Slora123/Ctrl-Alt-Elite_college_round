"""Typed payloads for every event type. Models are permissive about extra keys (events may grow
fields without breaking consumers) but strict about identity/required fields and types."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ..domain import EventType


class _P(BaseModel):
    model_config = ConfigDict(extra="allow")


class CellStartedP(_P):
    cell_id: str
    cell_version_id: str
    cell_run_id: str
    position: int
    timeout: float | None = None


class CellOutputP(_P):
    cell_id: str
    cell_run_id: str
    type: Literal["stream", "display"]
    name: Literal["stdout", "stderr"] | None = None
    text: str | None = None
    data: dict[str, Any] | None = None


class CellFinishedP(_P):
    cell_id: str
    cell_version_id: str
    cell_run_id: str
    status: str
    duration: float | None = None
    state_hash: str | None = None


class CellFailedP(_P):
    """Experiment failure (the cell raised / timed out / was interrupted). Infrastructure failures are
    reported as ``kernel_died`` instead."""
    cell_id: str
    cell_version_id: str
    cell_run_id: str
    exception_type: str
    message: str
    traceback: str = ""
    failing_line: str | None = None
    failing_lineno: int | None = None
    failure_kind: Literal["experiment"] | None = None


class CellEditedP(_P):
    change: Literal["created", "edited", "reverted", "deleted"]
    cell_id: str
    notebook_version: int
    cell_version_id: str | None = None
    author: Literal["human", "generator", "agent"] | None = None
    diff: str | None = None


class EnvChangedP(_P):
    op: str  # create | rebuild | install | pin | kernel_restart
    before_lockfile_hash: str | None = None
    after_lockfile_hash: str | None = None


class EnvInstallFailedP(_P):
    op: str
    failing_package: str | None = None
    conflict_details: str | None = None
    timed_out: bool = False
    error: str | None = None
    log_path: str | None = None


class KernelDiedP(_P):
    """Infrastructure failure: in-memory state was lost."""
    kind: str
    message: str
    exit_code: int | None = None
    cell_id: str | None = None
    failure_kind: Literal["infrastructure"] = "infrastructure"
    state_lost: bool = True


class RunStartedP(_P):
    run_id: str
    mode: Literal["cell", "until", "all", "clean"]
    seed: int
    notebook_version: int
    lockfile_hash: str | None = None
    verification_eligible: bool = False


class RunFinishedP(_P):
    run_id: str
    status: str
    failure_kind: Literal["experiment", "infrastructure"] | None = None
    message: str | None = None


class ActionBlockedP(_P):
    action_id: str
    kind: str
    decision: Literal["deny", "needs_approval"]
    reason: str = ""


class ActionResolvedP(_P):
    action_id: str
    decision: str
    reason: str = ""


class InputRequestedP(_P):
    cell_id: str
    cell_run_id: str
    prompt: str = ""
    password: bool = False


class CommMessageP(_P):
    msg_type: Literal["comm_open", "comm_msg", "comm_close"]
    content: dict[str, Any]


class CheckpointP(_P):
    checkpoint_id: str


class DatasetP(_P):
    dataset_id: str


PAYLOADS: dict[EventType, type[_P]] = {
    EventType.CELL_STARTED: CellStartedP, EventType.CELL_OUTPUT: CellOutputP,
    EventType.CELL_FINISHED: CellFinishedP, EventType.CELL_FAILED: CellFailedP,
    EventType.CELL_EDITED: CellEditedP, EventType.ENV_CHANGED: EnvChangedP,
    EventType.ENV_INSTALL_FAILED: EnvInstallFailedP, EventType.KERNEL_DIED: KernelDiedP,
    EventType.RUN_STARTED: RunStartedP, EventType.RUN_FINISHED: RunFinishedP,
    EventType.INPUT_REQUESTED: InputRequestedP, EventType.COMM_MESSAGE: CommMessageP,
    EventType.ACTION_BLOCKED: ActionBlockedP, EventType.ACTION_RESOLVED: ActionResolvedP,
    EventType.CHECKPOINT_SAVED: CheckpointP, EventType.CHECKPOINT_RESTORED: CheckpointP,
    EventType.DATASET_FETCH_STARTED: DatasetP, EventType.DATASET_FETCH_PROGRESS: DatasetP,
    EventType.DATASET_VERIFIED: DatasetP, EventType.DATASET_CACHED: DatasetP,
    EventType.DATASET_EVICTED: DatasetP, EventType.DATASET_FETCH_FAILED: DatasetP,
    EventType.DATASET_UPSTREAM_CHANGED: DatasetP,
}


def validate_payload(type: EventType, payload: dict[str, Any]) -> dict[str, Any]:
    """Raises ``pydantic.ValidationError`` if a producer emits a malformed event (a programming error)."""
    model = PAYLOADS.get(type)
    if model is None:
        return payload
    return model.model_validate(payload).model_dump(mode="json", exclude_unset=True)


def json_schemas() -> dict[str, Any]:
    return {t.value: m.model_json_schema() for t, m in PAYLOADS.items()}
