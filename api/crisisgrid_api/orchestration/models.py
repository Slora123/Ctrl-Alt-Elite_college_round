from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from crisisgrid_core.ids import new_id
from crisisgrid_core.models import Gap, RunResult, Verdict, utcnow
from crisisgrid_core.papers import ArtifactInventory, ParsedPaper
from crisisgrid_core.routing import RouteDecision

GateKind = Literal["data", "gap", "stuck", "final", "action"]

# system_design 3.3: what a human may answer at each kind of gate
ALLOWED_ACTIONS: dict[str, list[str]] = {
    "data": ["upload", "substitute", "stop_tier1"],
    "gap": ["approve_all", "approve", "reject", "edit"],
    "stuck": ["retry_with_hint", "edit_code", "skip_stage", "abort"],
    "final": ["accept", "request_rerun", "edit_caveats"],
    "action": ["approve", "edit", "reject"],
}


class Gate(BaseModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    kind: GateKind
    status: Literal["open", "resolved"] = "open"
    context: dict[str, Any] = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=list)
    action: str | None = None
    response: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class PaperRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    paper_hash: str
    filename: str = ""
    size: int = 0
    owner_id: str | None = None  # the user that uploaded it; None = created while authentication was off
    created_at: datetime = Field(default_factory=utcnow)


class AgentEdit(BaseModel):
    """A versioned change to a notebook cell. History is never rewritten: a revert would be a new edit."""

    id: str = Field(default_factory=new_id)
    cell_index: int
    old: str
    new: str
    author: Literal["agent", "human"] = "agent"
    reason: str = ""
    diff: str = ""
    risk_class: str = "safe"
    approved_by: Literal["policy", "human"] = "policy"
    created_at: datetime = Field(default_factory=utcnow)


RunStatus = Literal["running", "waiting_gate", "waiting_jobs", "completed", "failed"]


class RunState(BaseModel):
    """Everything the pipeline needs to resume after a restart. Persisted after every node."""

    run_id: str = Field(default_factory=new_id)
    paper_id: str
    status: RunStatus = "running"
    node: str = "ingest"
    waiting_gate_id: str | None = None
    error: str | None = None
    needs_ocr: bool = False
    parsed: ParsedPaper | None = None
    inventory: ArtifactInventory | None = None
    gaps: list[Gap] = Field(default_factory=list)
    route: RouteDecision | None = None
    data_resolution: Literal["upload", "substitute", "stopped"] | None = None
    assumptions_approved: bool = False
    hints: dict[str, str] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    notebook: list[dict[str, str]] | None = None  # cells to run (imported .ipynb); None = nothing to execute yet
    seeds: list[int] = Field(default_factory=lambda: [0, 1, 2])
    job_ids: list[str] = Field(default_factory=list)
    run_results: list[RunResult] = Field(default_factory=list)
    verdicts: list[Verdict] = Field(default_factory=list)
    edits: list[AgentEdit] = Field(default_factory=list)  # applied cell edits, oldest first
    pending_edit: AgentEdit | None = None  # proposed, waiting for an `action` gate answer
    diagnoses: list[dict[str, Any]] = Field(default_factory=list)
    execution: Literal["not_run", "clean_runs"] = "not_run"
    report: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
