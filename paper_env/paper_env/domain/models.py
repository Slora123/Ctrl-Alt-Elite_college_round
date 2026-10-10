from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .enums import (
    AccessType,
    ActionKind,
    Author,
    BlockedActionStatus,
    CellStatus,
    DatasetStatus,
    EventType,
    FailureKind,
    RunMode,
    RunStatus,
    SourceType,
    SplitProvenance,
)

__all__ = [
    "new_id", "utcnow", "content_hash", "make_dataset_id",
    "EnvSpec", "PaperEnv", "Cell", "CellVersion", "ErrorInfo",
    "Run", "CellRun", "DatasetFile", "DatasetSplit", "DatasetManifest",
    "Event", "BlockedAction", "Checkpoint", "UnserializableItem", "SessionLimits", "CellType",
]

CellType = Literal["code", "markdown"]


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_dataset_id(name: str, version: str) -> str:
    return f"{name}@{version}"


class EnvSpec(BaseModel):
    id: str = Field(default_factory=new_id)
    python_version: str = "3.12"
    requirements: list[str] = Field(default_factory=list)
    pins: dict[str, str] = Field(default_factory=dict)
    system_deps: list[str] = Field(default_factory=list)  # recorded only, never installed
    seed: int = 0
    lockfile_hash: str | None = None  # part of version identity; None until built


class SessionLimits(BaseModel):
    """Auto-disconnect and spend caps for a kernel session. ``None`` disables a limit."""

    idle_timeout_s: int | None = 1800
    max_session_s: int | None = 12 * 3600
    max_cost: float | None = None  # in the profile's currency units; checked against cost_per_hr * elapsed hours


class PaperEnv(BaseModel):
    id: str = Field(default_factory=new_id)
    alias: str
    paper_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    env_spec_id: str
    current_notebook_version: int = 0
    dataset_ids: list[str] = Field(default_factory=list)  # datasets every run of this paper needs
    owner_id: str | None = None  # principal that created it (None = created without authentication)
    runtime: str = "local-cpu"  # RuntimeProfile id, see backends/registry.py
    limits: SessionLimits = Field(default_factory=SessionLimits)


class Cell(BaseModel):
    id: str = Field(default_factory=new_id)
    paper_env_id: str
    position: int
    cell_type: CellType = "code"  # markdown cells are documentation: they never execute and never go stale
    node_id: str | None = None
    source_span: dict[str, Any] | None = None
    declared_inputs: list[str] = Field(default_factory=list)
    declared_outputs: list[str] = Field(default_factory=list)
    status: CellStatus = CellStatus.PENDING
    current_version_id: str | None = None
    input_warnings: list[str] = Field(default_factory=list)  # undeclared reads (best effort)
    last_failure_kind: FailureKind | None = None  # distinguishes infra failures from cell exceptions


class CellVersion(BaseModel):
    id: str = Field(default_factory=new_id)
    cell_id: str
    source: str
    author: Author
    parent_version_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    content_hash: str
    diff: str | None = None  # unified diff against the parent version
    notebook_version: int = 0


class ErrorInfo(BaseModel):
    type: str
    message: str
    traceback: str = ""
    failing_line: str | None = None
    failing_lineno: int | None = None


class Run(BaseModel):
    id: str = Field(default_factory=new_id)
    paper_env_id: str
    notebook_version: int
    env_lockfile_hash: str | None = None
    mode: RunMode
    seed: int
    backend: str = "local"
    hardware_info: dict[str, Any] = Field(default_factory=dict)  # python, platform, CUDA...
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.PENDING
    failure_kind: FailureKind | None = None
    verification_eligible: bool = False  # True only for run_clean
    approximate_split: bool = False
    kernel_session_id: str | None = None  # runs sharing a session share in-memory state
    dataset_manifests: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = None


class CellRun(BaseModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    cell_id: str
    cell_version_id: str
    status: CellStatus = CellStatus.PENDING
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    output_truncated: bool = False
    full_output_path: str | None = None
    error: ErrorInfo | None = None
    failure_kind: FailureKind | None = None
    duration: float | None = None
    state_hash: str | None = None
    blocked_action_id: str | None = None  # set when the pre-execute hook paused this cell


class DatasetFile(BaseModel):
    path: str
    size: int
    sha256: str


class DatasetSplit(BaseModel):
    name: str
    definition: str = ""
    provenance: SplitProvenance = SplitProvenance.APPROXIMATE


class DatasetManifest(BaseModel):
    dataset_id: str
    name: str
    version: str
    source_type: SourceType
    source_uri: str
    files: list[DatasetFile] = Field(default_factory=list)
    overall_checksum: str | None = None
    license: str | None = None
    access_type: AccessType = AccessType.PUBLIC
    splits: list[DatasetSplit] = Field(default_factory=list)
    status: DatasetStatus = DatasetStatus.REMOTE_ONLY
    parent_dataset_id: str | None = None
    producing_cell_version_id: str | None = None
    author: str | None = None
    run_id: str | None = None  # set for derived datasets
    remote_fingerprint: str | None = None  # ETag / revision sha seen at registration or last fetch
    cache_path: str | None = None
    size_bytes: int = 0
    last_accessed_at: datetime | None = None


class Event(BaseModel):
    id: str = Field(default_factory=new_id)
    seq: int | None = None  # assigned by the EventStore
    type: EventType
    ts: datetime = Field(default_factory=utcnow)
    paper_env_id: str | None = None
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class BlockedAction(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: ActionKind
    paper_env_id: str | None = None
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    decision: str  # needs_approval | deny
    reason: str = ""
    status: BlockedActionStatus = BlockedActionStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class UnserializableItem(BaseModel):
    name: str
    reason: str


class Checkpoint(BaseModel):
    id: str = Field(default_factory=new_id)
    paper_env_id: str
    run_id: str
    cell_id: str
    cell_version_id: str
    path: str
    saved: list[str] = Field(default_factory=list)
    unserializable: list[UnserializableItem] = Field(default_factory=list)
    rng_saved: bool = False
    cell_position: int = 0
    seed: int = 0
    notebook_version: int = 0
    cell_versions: dict[str, str] = Field(default_factory=dict)  # cell_id -> version id at save time
    file_hashes: dict[str, str] = Field(default_factory=dict)  # sha256 of payload files (integrity, not loaded by host)
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
