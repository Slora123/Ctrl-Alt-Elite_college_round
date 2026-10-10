"""Shared data models from system_design.md section 1 (Pydantic v2). Plain models: no persistence imports."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .ids import new_id

Provenance = Literal["stated", "inferred", "human"]
Author = Literal["human", "generator", "agent"]

# Cell authorship maps onto provenance: human -> human, generator/agent -> inferred.
AUTHOR_TO_PROVENANCE: dict[str, Provenance] = {"human": "human", "generator": "inferred", "agent": "inferred"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(BaseModel):
    kind: Literal["paper", "repo", "readme", "human"]
    ref: str
    quote: str | None = Field(default=None, max_length=200)


class Claim(BaseModel):
    id: str = Field(default_factory=new_id)
    text: str
    metric: str
    dataset: str
    split: str
    reported_value: float
    unit: Literal["percent", "fraction", "score", "other"]
    tolerance: float = Field(ge=0)
    baseline_value: float | None = None
    source: Source
    provenance: Provenance


class Gap(BaseModel):
    id: str = Field(default_factory=new_id)
    field: str
    severity: Literal["blocking", "major", "minor"]
    description: str
    suggested_resolution: str | None = None
    detected_by: Literal["rule", "agent"]


NodeType = Literal["dataset", "preprocess", "model", "train", "eval", "claim"]


class GraphNode(BaseModel):
    id: str
    type: NodeType
    spec: dict[str, Any] = Field(default_factory=dict)
    inputs: list[str] = Field(default_factory=list)
    provenance: Provenance
    sources: list[Source] = Field(default_factory=list)
    # sha256(canonical(spec) + input hashes + env digest + cell version hash if bound); filled by the graph service
    content_hash: str = ""


class ExperimentGraph(BaseModel):
    id: str = Field(default_factory=new_id)
    paper_id: str
    version: int = 1
    parent_version: int | None = None
    nodes: list[GraphNode] = Field(default_factory=list)
    env_digest: str | None = None
    created_by: Literal["agent", "human"]
    change_note: str | None = None

    def node(self, node_id: str) -> GraphNode:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)


class Assumption(BaseModel):
    id: str = Field(default_factory=new_id)
    node_id: str
    statement: str
    rationale: str
    status: Literal["proposed", "approved", "rejected"] = "proposed"


class RunResult(BaseModel):
    run_id: str
    graph_id: str
    seed: int
    status: Literal["ok", "failed", "timeout", "scaled_down", "infra_failed"]
    mode: Literal["interactive", "clean"]
    verification_eligible: bool
    approximate_split: bool = False
    metrics: dict[str, float] = Field(default_factory=dict)
    scale_note: str | None = None
    log_ref: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    lockfile_digest: str | None = None
    hardware_info: dict[str, Any] = Field(default_factory=dict)  # python, platform, cpu/gpu, cuda, driver
    dataset_manifest_ids: list[str] = Field(default_factory=list)
    notebook_version: int | None = None

    @model_validator(mode="after")
    def _only_clean_runs_are_eligible(self) -> "RunResult":
        if self.verification_eligible and self.mode != "clean":
            raise ValueError("verification_eligible requires mode='clean'; interactive runs are never evidence")
        return self


VerdictStatus = Literal["reproduced", "partial", "not_reproduced", "inconclusive", "not_executed"]


class Verdict(BaseModel):
    claim_id: str
    status: VerdictStatus
    reported: float
    measured_mean: float | None = None
    measured_std: float | None = None
    delta: float | None = None
    n_seeds: int = 0
    evidence_run_ids: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class EnvSpec(BaseModel):
    """The single EnvSpec definition. `digest` is the lockfile hash and part of node content_hash. Seed lives on Run."""

    id: str = Field(default_factory=new_id)
    python: str = "3.12"
    packages: list[str] = Field(default_factory=list)
    system_packages: list[str] = Field(default_factory=list)
    base_image: str | None = None
    gpu_required: bool = False
    build_commands: list[str] = Field(default_factory=list)
    digest: str | None = None


# ---- 1.1 Paper Environment models --------------------------------------------------------------------------------
# NOTE: paper_env/paper_env/domain/models.py already implements local versions of several of these with extra fields;
# reconciling the two is deferred (docs/decisions.md D-02).

CellStatusT = Literal["pending", "running", "success", "failed", "skipped", "stale", "blocked"]


class PaperEnv(BaseModel):
    id: str = Field(default_factory=new_id)
    paper_id: str
    alias: str
    paper_hash: str
    graph_id: str
    notebook_id: str
    env_spec_id: str
    created_at: datetime = Field(default_factory=utcnow)


class Notebook(BaseModel):
    id: str = Field(default_factory=new_id)
    paper_env_id: str
    version: int = 0


class Cell(BaseModel):
    id: str = Field(default_factory=new_id)
    notebook_id: str
    position: int
    node_id: str | None = None
    kind: Literal["code", "markdown"] = "code"
    source_span: dict[str, Any] | None = None
    declared_inputs: list[str] = Field(default_factory=list)
    declared_outputs: list[str] = Field(default_factory=list)
    status: CellStatusT = "pending"
    current_version_id: str


class CellVersion(BaseModel):
    id: str = Field(default_factory=new_id)
    cell_id: str
    source: str
    author: Author
    parent_version_id: str | None = None
    content_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class OutputItem(BaseModel):
    kind: str
    data: Any = None


class CellError(BaseModel):
    type: str
    message: str
    traceback: str = ""
    failing_line: str | None = None


class CellRun(BaseModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    cell_version_id: str
    status: Literal["success", "failed", "skipped", "infra_failed", "interrupted"]
    execution_count: int | None = None
    output_ref: str = ""
    output_preview: list[OutputItem] = Field(default_factory=list)
    error: CellError | None = None
    duration_s: float = 0.0
    state_hash: str | None = None


class KernelSession(BaseModel):
    id: str = Field(default_factory=new_id)
    notebook_id: str
    worker_id: str
    status: Literal["connecting", "idle", "busy", "dead", "disconnected"]
    started_at: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    resources: dict[str, Any] | None = None


class Checkpoint(BaseModel):
    id: str = Field(default_factory=new_id)
    kernel_session_id: str
    after_cell_version_id: str
    storage_ref: str
    serialized: list[str] = Field(default_factory=list)
    not_serialized: list[str] = Field(default_factory=list)  # explicit record of what was lost
    rng_state_saved: bool = False


class FileEntry(BaseModel):
    path: str
    size: int
    sha256: str


class SplitDef(BaseModel):
    name: str
    definition: dict[str, Any] = Field(default_factory=dict)
    provenance: Literal["paper_official", "approximate"]


class DatasetManifest(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    revision: str
    source_type: Literal["huggingface", "kaggle", "url", "s3", "user_upload", "local_path"]
    source_uri: str
    files: list[FileEntry] = Field(default_factory=list)
    overall_checksum: str = ""
    license: str | None = None
    access_type: Literal["public", "gated", "user_uploaded"] = "public"
    splits: list[SplitDef] = Field(default_factory=list)
    status: Literal["remote_only", "downloading", "cached", "verified", "corrupted", "upstream_changed"] = "remote_only"
    parent_dataset_id: str | None = None
    producing_cell_version_id: str | None = None
    author: Author = "human"
