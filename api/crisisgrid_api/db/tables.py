from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")


class RunEventRow(Base):
    __tablename__ = "run_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUIDv7: sorts by creation time
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_run_events_run_id_id", "run_id", "id"),)


class LLMCallRow(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    messages: Mapped[list[Any]] = mapped_column(_JSON, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_llm_calls_run_id", "run_id"),)


class PaperRow(Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    paper_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pdf: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # TODO: S3 (MinIO) once the storage layer exists
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # user that uploaded it
    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None for papers uploaded before migration 0006
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PipelineRunRow(Base):
    __tablename__ = "pipeline_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    node: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GateRow(Base):
    __tablename__ = "gates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    allowed_actions: Mapped[list[Any]] = mapped_column(_JSON, nullable=False)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerRow(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spec: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="researcher")
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiTokenRow(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class GraphVersionRow(Base):
    """One immutable version of a paper's experiment graph, plus the notebook cells bound to its nodes."""
    __tablename__ = "graph_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(16), nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    cells: Mapped[list[Any]] = mapped_column(_JSON, nullable=False)  # ordered [{node_id, source}]
    run_ids: Mapped[list[Any]] = mapped_column(_JSON, nullable=False)  # pipeline runs started from this version
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_graph_versions_paper_version", "paper_id", "version", unique=True),)
