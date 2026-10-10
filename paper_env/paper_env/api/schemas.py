from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain import Author, DatasetSplit, SourceType, AccessType, DatasetFile


class CreatePaperEnv(BaseModel):
    alias: str
    paper_hash: str
    python_version: str = "3.12"
    requirements: list[str] = Field(default_factory=list)
    pins: dict[str, str] = Field(default_factory=dict)
    system_deps: list[str] = Field(default_factory=list)
    seed: int = 0
    dataset_ids: list[str] = Field(default_factory=list)
    build_env: bool = False


class SetDatasets(BaseModel):
    dataset_ids: list[str]


class InstallReq(BaseModel):
    name: str
    version: str | None = None


class PinReq(BaseModel):
    name: str
    version: str


class AddCell(BaseModel):
    source: str
    author: Author = Author.HUMAN
    position: int | None = None
    declared_inputs: list[str] = Field(default_factory=list)
    declared_outputs: list[str] = Field(default_factory=list)
    node_id: str | None = None
    source_span: dict[str, Any] | None = None


class EditCell(BaseModel):
    source: str
    author: Author


class Declarations(BaseModel):
    declared_inputs: list[str] | None = None
    declared_outputs: list[str] | None = None


class RevertReq(BaseModel):
    author: Author = Author.HUMAN


class StartRun(BaseModel):
    paper_env_id: str
    mode: Literal["cell", "until", "all", "clean"]
    cell_id: str | None = None  # required for cell / until
    stop_on_fail: bool = True
    seed: int | None = None
    cell_timeout: float | None = None
    run_timeout: float | None = None
    checkpoint: bool | None = None
    wait: bool = False  # block until the run finishes instead of returning 202 immediately


class RegisterDataset(BaseModel):
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
    author: str | None = None


class UploadDataset(BaseModel):
    name: str
    version: str
    path: str
    author: str | None = None
    license: str | None = None
    splits: list[DatasetSplit] = Field(default_factory=list)


class ResetDataset(BaseModel):
    accept_upstream: bool = False


class ResumeReq(BaseModel):
    continue_run: bool = False
    stop_on_fail: bool = True


class ResolveBlocked(BaseModel):
    decision: Literal["allow", "deny"]
    reason: str = ""


class CodePos(BaseModel):
    code: str
    cursor_pos: int


class CommReq(BaseModel):
    msg_type: Literal["comm_open", "comm_msg", "comm_close"] = "comm_msg"
    content: dict[str, Any]
    buffers: list[str] = Field(default_factory=list)  # base64


class InputReq(BaseModel):
    text: str
