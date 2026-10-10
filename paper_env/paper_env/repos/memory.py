"""In-memory repo implementation. Used for fast service tests and as the contract-test twin of SQLite."""
from __future__ import annotations

import copy
import threading
from typing import Sequence

from ..domain import (
    BlockedAction, BlockedActionStatus, Cell, CellRun, CellVersion, Checkpoint,
    DatasetManifest, EnvSpec, Event, EventType, PaperEnv, Run,
)
from .interfaces import Conflict, NotFound


class MemoryStore:
    """Shared state behind all UnitOfWork instances of one 'database'."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.data: dict[str, dict] = {
            k: {} for k in (
                "envs", "specs", "cells", "versions", "runs", "cell_runs",
                "datasets", "events", "checkpoints", "blocked",
            )
        }
        self.usage: set[tuple[str, str]] = set()
        self.event_seq = 0

    def snapshot(self):
        return copy.deepcopy((self.data, self.usage, self.event_seq))

    def restore(self, snap) -> None:
        self.data, self.usage, self.event_seq = snap


def _get(table: dict, key: str, what: str):
    try:
        return copy.deepcopy(table[key])
    except KeyError:
        raise NotFound(f"{what} {key!r} not found") from None


class _Base:
    def __init__(self, store: MemoryStore):
        self.s = store
        self.d = store.data


class MemPaperEnvRepo(_Base):
    def add(self, env: PaperEnv, spec: EnvSpec) -> None:
        if any(e.alias == env.alias for e in self.d["envs"].values()):
            raise Conflict(f"alias {env.alias!r} already exists")
        self.d["envs"][env.id] = env.model_copy(deep=True)
        self.d["specs"][spec.id] = spec.model_copy(deep=True)

    def get(self, paper_env_id): return _get(self.d["envs"], paper_env_id, "PaperEnv")

    def get_by_alias(self, alias):
        for e in self.d["envs"].values():
            if e.alias == alias:
                return e.model_copy(deep=True)
        raise NotFound(f"PaperEnv alias {alias!r} not found")

    def list(self): return sorted((e.model_copy(deep=True) for e in self.d["envs"].values()), key=lambda e: e.created_at)

    def update(self, env):
        self.get(env.id)
        self.d["envs"][env.id] = env.model_copy(deep=True)

    def delete(self, paper_env_id):
        env = self.get(paper_env_id)
        del self.d["envs"][paper_env_id]
        self.d["specs"].pop(env.env_spec_id, None)
        for cid in [c.id for c in self.d["cells"].values() if c.paper_env_id == paper_env_id]:
            self.s_delete_cell(cid)
        for rid in [r.id for r in self.d["runs"].values() if r.paper_env_id == paper_env_id]:
            del self.d["runs"][rid]
            for k in [k for k, v in self.d["cell_runs"].items() if v.run_id == rid]:
                del self.d["cell_runs"][k]

    def s_delete_cell(self, cell_id):
        self.d["cells"].pop(cell_id, None)
        for k in [k for k, v in self.d["versions"].items() if v.cell_id == cell_id]:
            del self.d["versions"][k]

    def get_spec(self, spec_id): return _get(self.d["specs"], spec_id, "EnvSpec")

    def update_spec(self, spec):
        self.get_spec(spec.id)
        self.d["specs"][spec.id] = spec.model_copy(deep=True)


class MemCellRepo(_Base):
    def add_cell(self, cell): self.d["cells"][cell.id] = cell.model_copy(deep=True)
    def get_cell(self, cell_id): return _get(self.d["cells"], cell_id, "Cell")

    def list_cells(self, paper_env_id):
        return sorted((c.model_copy(deep=True) for c in self.d["cells"].values() if c.paper_env_id == paper_env_id),
                      key=lambda c: c.position)

    def update_cell(self, cell):
        self.get_cell(cell.id)
        self.d["cells"][cell.id] = cell.model_copy(deep=True)

    def delete_cell(self, cell_id):
        self.get_cell(cell_id)
        self.d["cells"].pop(cell_id)
        for k in [k for k, v in self.d["versions"].items() if v.cell_id == cell_id]:
            del self.d["versions"][k]

    def add_version(self, version): self.d["versions"][version.id] = version.model_copy(deep=True)
    def get_version(self, version_id): return _get(self.d["versions"], version_id, "CellVersion")

    def list_versions(self, cell_id):
        return sorted((v.model_copy(deep=True) for v in self.d["versions"].values() if v.cell_id == cell_id),
                      key=lambda v: v.created_at)


class MemRunRepo(_Base):
    def add_run(self, run): self.d["runs"][run.id] = run.model_copy(deep=True)
    def get_run(self, run_id): return _get(self.d["runs"], run_id, "Run")

    def update_run(self, run):
        self.get_run(run.id)
        self.d["runs"][run.id] = run.model_copy(deep=True)

    def list_runs(self, paper_env_id):
        runs = (r.model_copy(deep=True) for r in self.d["runs"].values() if r.paper_env_id == paper_env_id)
        return sorted(runs, key=lambda r: (r.started_at is None, r.started_at))

    def add_cell_run(self, cell_run): self.d["cell_runs"][cell_run.id] = cell_run.model_copy(deep=True)

    def update_cell_run(self, cell_run):
        _get(self.d["cell_runs"], cell_run.id, "CellRun")
        self.d["cell_runs"][cell_run.id] = cell_run.model_copy(deep=True)

    def list_cell_runs(self, run_id):
        # insertion order == execution order
        return [c.model_copy(deep=True) for c in self.d["cell_runs"].values() if c.run_id == run_id]


class MemDatasetRepo(_Base):
    def upsert(self, manifest): self.d["datasets"][manifest.dataset_id] = manifest.model_copy(deep=True)
    def get(self, dataset_id): return _get(self.d["datasets"], dataset_id, "Dataset")
    def exists(self, dataset_id): return dataset_id in self.d["datasets"]
    def list(self): return [m.model_copy(deep=True) for m in self.d["datasets"].values()]

    def delete(self, dataset_id):
        self.get(dataset_id)
        del self.d["datasets"][dataset_id]
        self.s.usage = {u for u in self.s.usage if u[1] != dataset_id}

    def mark_in_use(self, run_id, dataset_id): self.s.usage.add((run_id, dataset_id))
    def release_run(self, run_id): self.s.usage = {u for u in self.s.usage if u[0] != run_id}
    def in_use_dataset_ids(self): return {d for _, d in self.s.usage}


class MemEventStore(_Base):
    def append(self, event):
        self.s.event_seq += 1
        e = event.model_copy(deep=True, update={"seq": self.s.event_seq})
        self.d["events"][e.id] = e
        return e.model_copy(deep=True)

    def list(self, after_seq=0, limit=1000, run_id=None, paper_env_id=None, types: Sequence[EventType] | None = None):
        out = []
        for e in sorted(self.d["events"].values(), key=lambda e: e.seq):
            if e.seq <= after_seq: continue
            if run_id and e.run_id != run_id: continue
            if paper_env_id and e.paper_env_id != paper_env_id: continue
            if types and e.type not in types: continue
            out.append(e.model_copy(deep=True))
            if len(out) >= limit: break
        return out


class MemCheckpointRepo(_Base):
    def add(self, checkpoint): self.d["checkpoints"][checkpoint.id] = checkpoint.model_copy(deep=True)
    def get(self, checkpoint_id): return _get(self.d["checkpoints"], checkpoint_id, "Checkpoint")

    def list(self, paper_env_id, run_id=None):
        return sorted((c.model_copy(deep=True) for c in self.d["checkpoints"].values()
                       if c.paper_env_id == paper_env_id and (run_id is None or c.run_id == run_id)),
                      key=lambda c: c.created_at)


class MemBlockedActionRepo(_Base):
    def add(self, action): self.d["blocked"][action.id] = action.model_copy(deep=True)
    def get(self, action_id): return _get(self.d["blocked"], action_id, "BlockedAction")

    def update(self, action):
        self.get(action.id)
        self.d["blocked"][action.id] = action.model_copy(deep=True)

    def list_pending(self, paper_env_id=None):
        return sorted((a.model_copy(deep=True) for a in self.d["blocked"].values()
                       if a.status == BlockedActionStatus.PENDING
                       and (paper_env_id is None or a.paper_env_id == paper_env_id)),
                      key=lambda a: a.created_at)


class MemoryUnitOfWork:
    def __init__(self, store: MemoryStore):
        self._store = store

    def __enter__(self):
        self._store.lock.acquire()
        self._snap = self._store.snapshot()
        s = self._store
        self.paper_envs = MemPaperEnvRepo(s)
        self.cells = MemCellRepo(s)
        self.runs = MemRunRepo(s)
        self.datasets = MemDatasetRepo(s)
        self.events = MemEventStore(s)
        self.checkpoints = MemCheckpointRepo(s)
        self.blocked = MemBlockedActionRepo(s)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self._store.restore(self._snap)
        finally:
            self._store.lock.release()


def memory_uow_factory():
    store = MemoryStore()
    return lambda: MemoryUnitOfWork(store)
