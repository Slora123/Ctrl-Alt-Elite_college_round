"""ORM tables. The ONLY place (with the rest of repos/sqlite) that knows about SQLAlchemy."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator):
    """SQLite drops tzinfo; always store UTC and return aware datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class EnvSpecRow(Base):
    __tablename__ = "env_specs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    python_version: Mapped[str] = mapped_column(String)
    requirements: Mapped[list] = mapped_column(JSON)
    pins: Mapped[dict] = mapped_column(JSON)
    system_deps: Mapped[list] = mapped_column(JSON)
    seed: Mapped[int] = mapped_column(Integer)
    lockfile_hash: Mapped[str | None] = mapped_column(String, nullable=True)


class PaperEnvRow(Base):
    __tablename__ = "paper_envs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    alias: Mapped[str] = mapped_column(String, unique=True)
    paper_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    env_spec_id: Mapped[str] = mapped_column(ForeignKey("env_specs.id"))
    current_notebook_version: Mapped[int] = mapped_column(Integer)


class CellRow(Base):
    __tablename__ = "cells"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    paper_env_id: Mapped[str] = mapped_column(ForeignKey("paper_envs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_span: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    declared_inputs: Mapped[list] = mapped_column(JSON)
    declared_outputs: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String)
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    input_warnings: Mapped[list] = mapped_column(JSON)


class CellVersionRow(Base):
    __tablename__ = "cell_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    cell_id: Mapped[str] = mapped_column(ForeignKey("cells.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String)
    parent_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    content_hash: Mapped[str] = mapped_column(String)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    notebook_version: Mapped[int] = mapped_column(Integer)


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    paper_env_id: Mapped[str] = mapped_column(ForeignKey("paper_envs.id", ondelete="CASCADE"), index=True)
    notebook_version: Mapped[int] = mapped_column(Integer)
    env_lockfile_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    mode: Mapped[str] = mapped_column(String)
    seed: Mapped[int] = mapped_column(Integer)
    backend: Mapped[str] = mapped_column(String)
    hardware_info: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String)
    failure_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    verification_eligible: Mapped[bool] = mapped_column(Boolean)
    approximate_split: Mapped[bool] = mapped_column(Boolean)
    dataset_manifests: Mapped[list] = mapped_column(JSON)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class CellRunRow(Base):
    __tablename__ = "cell_runs"
    # rowid order == execution order; `ordinal` makes that explicit and portable
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, unique=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    cell_id: Mapped[str] = mapped_column(String)
    cell_version_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    outputs: Mapped[list] = mapped_column(JSON)
    output_truncated: Mapped[bool] = mapped_column(Boolean)
    full_output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    state_hash: Mapped[str | None] = mapped_column(String, nullable=True)


class DatasetRow(Base):
    __tablename__ = "datasets"
    dataset_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    source_type: Mapped[str] = mapped_column(String)
    source_uri: Mapped[str] = mapped_column(String)
    files: Mapped[list] = mapped_column(JSON)
    overall_checksum: Mapped[str | None] = mapped_column(String, nullable=True)
    license: Mapped[str | None] = mapped_column(String, nullable=True)
    access_type: Mapped[str] = mapped_column(String)
    splits: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String)
    parent_dataset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    producing_cell_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    remote_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    cache_path: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    last_accessed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class DatasetUsageRow(Base):
    __tablename__ = "dataset_usage"
    __table_args__ = (UniqueConstraint("run_id", "dataset_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    dataset_id: Mapped[str] = mapped_column(String, index=True)


class EventRow(Base):
    __tablename__ = "events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, unique=True)
    type: Mapped[str] = mapped_column(String, index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    paper_env_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (Index("ix_events_run", "run_id", "seq"), Index("ix_events_env", "paper_env_id", "seq"))


class CheckpointRow(Base):
    __tablename__ = "checkpoints"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    paper_env_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    cell_id: Mapped[str] = mapped_column(String)
    cell_version_id: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    saved: Mapped[list] = mapped_column(JSON)
    unserializable: Mapped[list] = mapped_column(JSON)
    rng_saved: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class BlockedActionRow(Base):
    __tablename__ = "blocked_actions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    paper_env_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
