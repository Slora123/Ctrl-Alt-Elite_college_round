from __future__ import annotations

from typing import Sequence, TypeVar

from pydantic import BaseModel
from sqlalchemy import JSON, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ...domain import (
    BlockedAction, BlockedActionStatus, Cell, CellRun, CellVersion, Checkpoint,
    DatasetManifest, EnvSpec, Event, EventType, PaperEnv, Run,
)
from ..interfaces import Conflict, NotFound
from . import tables as t

M = TypeVar("M", bound=BaseModel)


def to_row(model: BaseModel, row_cls, **extra):
    """Domain -> ORM row. JSON columns get json-mode dumps, scalars keep python types."""
    py, js = model.model_dump(), model.model_dump(mode="json")
    values = {}
    for col in row_cls.__table__.columns:
        if col.name in extra:
            values[col.name] = extra[col.name]
        elif col.name in py:
            values[col.name] = js[col.name] if isinstance(col.type, JSON) else py[col.name]
    return row_cls(**values)


def from_row(row, model_cls: type[M]) -> M:
    return model_cls.model_validate({c.name: getattr(row, c.name) for c in row.__table__.columns
                                     if c.name in model_cls.model_fields})


def apply(model: BaseModel, row) -> None:
    """Copy all model fields onto an existing row."""
    new = to_row(model, type(row))
    for name in type(model).model_fields:
        if name in row.__table__.columns:
            setattr(row, name, getattr(new, name))


class _Base:
    def __init__(self, s: Session):
        self.s = s

    def _get(self, row_cls, key, what: str):
        row = self.s.get(row_cls, key)
        if row is None:
            raise NotFound(f"{what} {key!r} not found")
        return row

    def _flush(self):
        try:
            self.s.flush()
        except IntegrityError as e:
            raise Conflict(str(e.orig)) from e


class SqlPaperEnvRepo(_Base):
    def add(self, env: PaperEnv, spec: EnvSpec) -> None:
        self.s.add(to_row(spec, t.EnvSpecRow))
        self._flush()
        self.s.add(to_row(env, t.PaperEnvRow))
        self._flush()

    def get(self, paper_env_id): return from_row(self._get(t.PaperEnvRow, paper_env_id, "PaperEnv"), PaperEnv)

    def get_by_alias(self, alias):
        row = self.s.scalar(select(t.PaperEnvRow).where(t.PaperEnvRow.alias == alias))
        if row is None:
            raise NotFound(f"PaperEnv alias {alias!r} not found")
        return from_row(row, PaperEnv)

    def list(self):
        return [from_row(r, PaperEnv) for r in self.s.scalars(select(t.PaperEnvRow).order_by(t.PaperEnvRow.created_at))]

    def update(self, env):
        apply(env, self._get(t.PaperEnvRow, env.id, "PaperEnv"))
        self._flush()

    def delete(self, paper_env_id):
        row = self._get(t.PaperEnvRow, paper_env_id, "PaperEnv")
        spec_id = row.env_spec_id
        # explicit cascade (FKs cascade too, but cell/versions/runs reference by ORM-less FKs)
        self.s.execute(delete(t.CellRow).where(t.CellRow.paper_env_id == paper_env_id))
        self.s.execute(delete(t.RunRow).where(t.RunRow.paper_env_id == paper_env_id))
        self.s.delete(row)
        self.s.flush()
        spec = self.s.get(t.EnvSpecRow, spec_id)
        if spec is not None:
            self.s.delete(spec)
        self.s.flush()

    def get_spec(self, spec_id): return from_row(self._get(t.EnvSpecRow, spec_id, "EnvSpec"), EnvSpec)

    def update_spec(self, spec):
        apply(spec, self._get(t.EnvSpecRow, spec.id, "EnvSpec"))
        self._flush()


class SqlCellRepo(_Base):
    def add_cell(self, cell):
        self.s.add(to_row(cell, t.CellRow))
        self._flush()

    def get_cell(self, cell_id): return from_row(self._get(t.CellRow, cell_id, "Cell"), Cell)

    def list_cells(self, paper_env_id):
        q = select(t.CellRow).where(t.CellRow.paper_env_id == paper_env_id).order_by(t.CellRow.position)
        return [from_row(r, Cell) for r in self.s.scalars(q)]

    def update_cell(self, cell):
        apply(cell, self._get(t.CellRow, cell.id, "Cell"))
        self._flush()

    def delete_cell(self, cell_id):
        self.s.delete(self._get(t.CellRow, cell_id, "Cell"))
        self.s.flush()

    def add_version(self, version):
        self.s.add(to_row(version, t.CellVersionRow))
        self._flush()

    def get_version(self, version_id): return from_row(self._get(t.CellVersionRow, version_id, "CellVersion"), CellVersion)

    def list_versions(self, cell_id):
        q = select(t.CellVersionRow).where(t.CellVersionRow.cell_id == cell_id).order_by(t.CellVersionRow.created_at)
        return [from_row(r, CellVersion) for r in self.s.scalars(q)]


class SqlRunRepo(_Base):
    def add_run(self, run):
        self.s.add(to_row(run, t.RunRow))
        self._flush()

    def get_run(self, run_id): return from_row(self._get(t.RunRow, run_id, "Run"), Run)

    def update_run(self, run):
        apply(run, self._get(t.RunRow, run.id, "Run"))
        self._flush()

    def list_runs(self, paper_env_id):
        q = select(t.RunRow).where(t.RunRow.paper_env_id == paper_env_id).order_by(
            t.RunRow.started_at.is_(None), t.RunRow.started_at)
        return [from_row(r, Run) for r in self.s.scalars(q)]

    def add_cell_run(self, cell_run):
        self.s.add(to_row(cell_run, t.CellRunRow))
        self._flush()

    def _cr(self, cell_run_id):
        row = self.s.scalar(select(t.CellRunRow).where(t.CellRunRow.id == cell_run_id))
        if row is None:
            raise NotFound(f"CellRun {cell_run_id!r} not found")
        return row

    def update_cell_run(self, cell_run):
        apply(cell_run, self._cr(cell_run.id))
        self._flush()

    def list_cell_runs(self, run_id):
        q = select(t.CellRunRow).where(t.CellRunRow.run_id == run_id).order_by(t.CellRunRow.ordinal)
        return [from_row(r, CellRun) for r in self.s.scalars(q)]


class SqlDatasetRepo(_Base):
    def upsert(self, manifest):
        row = self.s.get(t.DatasetRow, manifest.dataset_id)
        if row is None:
            self.s.add(to_row(manifest, t.DatasetRow))
        else:
            apply(manifest, row)
        self._flush()

    def get(self, dataset_id): return from_row(self._get(t.DatasetRow, dataset_id, "Dataset"), DatasetManifest)
    def exists(self, dataset_id): return self.s.get(t.DatasetRow, dataset_id) is not None
    def list(self): return [from_row(r, DatasetManifest) for r in self.s.scalars(select(t.DatasetRow))]

    def delete(self, dataset_id):
        self.s.delete(self._get(t.DatasetRow, dataset_id, "Dataset"))
        self.s.execute(delete(t.DatasetUsageRow).where(t.DatasetUsageRow.dataset_id == dataset_id))
        self.s.flush()

    def mark_in_use(self, run_id, dataset_id):
        exists = self.s.scalar(select(t.DatasetUsageRow.id).where(
            t.DatasetUsageRow.run_id == run_id, t.DatasetUsageRow.dataset_id == dataset_id))
        if exists is None:
            self.s.add(t.DatasetUsageRow(run_id=run_id, dataset_id=dataset_id))
            self.s.flush()

    def release_run(self, run_id):
        self.s.execute(delete(t.DatasetUsageRow).where(t.DatasetUsageRow.run_id == run_id))
        self.s.flush()

    def in_use_dataset_ids(self):
        return set(self.s.scalars(select(t.DatasetUsageRow.dataset_id).distinct()))


class SqlEventStore(_Base):
    def append(self, event):
        row = to_row(event, t.EventRow, seq=None)
        self.s.add(row)
        self.s.flush()
        return event.model_copy(update={"seq": row.seq})

    def list(self, after_seq=0, limit=1000, run_id=None, paper_env_id=None, types: Sequence[EventType] | None = None):
        q = select(t.EventRow).where(t.EventRow.seq > after_seq)
        if run_id: q = q.where(t.EventRow.run_id == run_id)
        if paper_env_id: q = q.where(t.EventRow.paper_env_id == paper_env_id)
        if types: q = q.where(t.EventRow.type.in_([str(x) for x in types]))
        return [from_row(r, Event) for r in self.s.scalars(q.order_by(t.EventRow.seq).limit(limit))]


class SqlCheckpointRepo(_Base):
    def add(self, checkpoint):
        self.s.add(to_row(checkpoint, t.CheckpointRow))
        self._flush()

    def get(self, checkpoint_id): return from_row(self._get(t.CheckpointRow, checkpoint_id, "Checkpoint"), Checkpoint)

    def list(self, paper_env_id, run_id=None):
        q = select(t.CheckpointRow).where(t.CheckpointRow.paper_env_id == paper_env_id)
        if run_id: q = q.where(t.CheckpointRow.run_id == run_id)
        return [from_row(r, Checkpoint) for r in self.s.scalars(q.order_by(t.CheckpointRow.created_at))]


class SqlBlockedActionRepo(_Base):
    def add(self, action):
        self.s.add(to_row(action, t.BlockedActionRow))
        self._flush()

    def get(self, action_id): return from_row(self._get(t.BlockedActionRow, action_id, "BlockedAction"), BlockedAction)

    def update(self, action):
        apply(action, self._get(t.BlockedActionRow, action.id, "BlockedAction"))
        self._flush()

    def list_pending(self, paper_env_id=None):
        q = select(t.BlockedActionRow).where(t.BlockedActionRow.status == BlockedActionStatus.PENDING.value)
        if paper_env_id: q = q.where(t.BlockedActionRow.paper_env_id == paper_env_id)
        return [from_row(r, BlockedAction) for r in self.s.scalars(q.order_by(t.BlockedActionRow.created_at))]


class SqliteUnitOfWork:
    def __init__(self, session_factory: sessionmaker):
        self._sf = session_factory

    def __enter__(self):
        self.session = self._sf()
        s = self.session
        self.paper_envs = SqlPaperEnvRepo(s)
        self.cells = SqlCellRepo(s)
        self.runs = SqlRunRepo(s)
        self.datasets = SqlDatasetRepo(s)
        self.events = SqlEventStore(s)
        self.checkpoints = SqlCheckpointRepo(s)
        self.blocked = SqlBlockedActionRepo(s)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
