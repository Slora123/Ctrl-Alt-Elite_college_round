"""Checkpoint & resume.

Serialization/deserialization happens inside the kernel only (see ``_paper_env_rt``). This service
handles paths, JSON reports and file hashes, and decides what is stale after a resume. It never
loads a pickle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..backends import BackendError, CheckpointRef, ComputeBackend
from ..config import Settings
from ..datasets.checksums import sha256_file
from ..domain import CellStatus, Checkpoint, CellRun, EventType, Run, UnserializableItem
from ..events import EventBus
from ..kernel.runner import KernelRunner, SessionError
from ..repos import UnitOfWorkFactory
from .notebook import NotebookService


class CheckpointError(RuntimeError):
    pass


@dataclass
class ResumeResult:
    checkpoint_id: str
    session_id: str
    restored: list[str]
    not_restored: list[dict[str, str]]  # [{name, reason}]
    rng_restored: list[str]
    stale_cells: list[str]
    resume_after_position: int
    messages: list[str] = field(default_factory=list)  # explicit, human-readable statements of what was lost

    prefix_ok: bool = True  # every cell at/before the checkpoint position is intact

    @property
    def complete(self) -> bool:
        """Everything the checkpoint promised was restored. Cells after the checkpoint are expected to be
        stale (they still have to run) and do not count against this."""
        return not self.not_restored and self.prefix_ok


class CheckpointService:
    def __init__(self, settings: Settings, uow_factory: UnitOfWorkFactory, bus: EventBus, backend: ComputeBackend,
                 runner: KernelRunner, notebook: NotebookService, kernel_opts):
        self.settings, self._uow, self.bus, self.backend = settings, uow_factory, bus, backend
        self.runner, self.notebook = runner, notebook
        self._kernel_opts = kernel_opts  # callable(run_like_id, env) -> provision opts (workdir, dataset mount)

    # ------------------------------------------------------------------ save
    def save(self, run: Run, cell_run: CellRun) -> Checkpoint | None:
        """Checkpoint everything declared as an output by this cell and all cells above it, plus RNG state."""
        session = self.runner.get_session(run.paper_env_id)
        if session is None or not session.alive:
            return None
        with self._uow() as u:
            cells = u.cells.list_cells(run.paper_env_id)
            pos = next(c.position for c in cells if c.id == cell_run.cell_id)
            upto = [c for c in cells if c.position <= pos]
            names = list(dict.fromkeys(n for c in upto for n in c.declared_outputs))
            versions = {c.id: c.current_version_id for c in upto}
        cp = Checkpoint(paper_env_id=run.paper_env_id, run_id=run.id, cell_id=cell_run.cell_id,
                        cell_version_id=cell_run.cell_version_id, path="", cell_position=pos, seed=run.seed,
                        notebook_version=run.notebook_version, cell_versions=versions)
        dest = self.settings.checkpoints_dir(run.paper_env_id) / cp.id
        try:
            ref = self.backend.snapshot(session.handle, dest=dest, names=names)
        except BackendError as e:
            self.bus.publish(EventType.CHECKPOINT_SAVED, {
                "checkpoint_id": cp.id, "ok": False, "error": str(e), "cell_id": cell_run.cell_id},
                paper_env_id=run.paper_env_id, run_id=run.id)
            return None
        cp.path = ref.path
        cp.saved = ref.saved
        cp.unserializable = [UnserializableItem(**x) for x in ref.unserializable]
        cp.rng_saved = ref.rng_saved
        cp.notes = list(getattr(ref, "notes", []))
        if not names:
            cp.notes.append("no cell declared any output, so no variables were checkpointed (RNG state only)")
        cp.file_hashes = {f.name: sha256_file(f) for f in sorted(dest.iterdir()) if f.is_file()}
        with self._uow() as u:
            u.checkpoints.add(cp)
        self.bus.publish(EventType.CHECKPOINT_SAVED, {
            "checkpoint_id": cp.id, "ok": True, "cell_id": cell_run.cell_id, "saved": cp.saved,
            "unserializable": [x.model_dump() for x in cp.unserializable], "rng_saved": cp.rng_saved,
            "notes": cp.notes}, paper_env_id=run.paper_env_id, run_id=run.id)
        return cp

    def get(self, checkpoint_id: str) -> Checkpoint:
        with self._uow() as u:
            return u.checkpoints.get(checkpoint_id)

    def list(self, paper_env_id: str, run_id: str | None = None) -> list[Checkpoint]:
        with self._uow() as u:
            return u.checkpoints.list(paper_env_id, run_id)

    # ------------------------------------------------------------------ resume
    def verify_files(self, cp: Checkpoint) -> None:
        root = Path(cp.path)
        for name, digest in cp.file_hashes.items():
            f = root / name
            if not f.is_file() or sha256_file(f) != digest:
                raise CheckpointError(f"checkpoint {cp.id} is corrupted or was modified: {name}")

    def resume_from(self, checkpoint_id: str) -> ResumeResult:
        """Fresh kernel + restored variables + restored RNG. Anything that could not be restored makes the
        affected cells and everything after them ``stale``, and the result says so explicitly. In-memory
        state that was not checkpointed is NOT assumed to exist."""
        cp = self.get(checkpoint_id)
        self.verify_files(cp)  # hash check on the host: no unpickling here, just bytes
        with self._uow() as u:
            env = u.paper_envs.get(cp.paper_env_id)
            cells = u.cells.list_cells(cp.paper_env_id)
        opts = self._kernel_opts(f"resume-{cp.id}", env)
        try:
            session = self.runner.start_session(cp.paper_env_id, cp.seed, **opts)
            report = self.backend.restore(session.handle, CheckpointRef(id=cp.id, path=cp.path))
        except (SessionError, BackendError) as e:
            raise CheckpointError(f"could not resume from {cp.id}: {e}") from e

        restored = set(report["restored"])
        not_restored = [x.model_dump() for x in cp.unserializable] + list(report["failed"])
        if report.get("import_failed"):
            msgs_imports = "imports that could not be re-established: " + ", ".join(
                f"{x['name']} ({x['reason']})" for x in report["import_failed"])
        else:
            msgs_imports = None
        bad_names = {x["name"] for x in not_restored}
        msgs: list[str] = []
        if msgs_imports:
            msgs.append(msgs_imports)
            not_restored += report["import_failed"]
        if not_restored:
            msgs.append("these variables could not be restored: " + ", ".join(
                f"{x['name']} ({x['reason']})" for x in not_restored))
        if cp.rng_saved and not report["rng_restored"]:
            msgs.append(f"RNG state could not be restored: {report.get('rng_error')}; results may differ from an uninterrupted run")
        elif not cp.rng_saved:
            msgs.append("RNG state was not saved in this checkpoint; results may differ from an uninterrupted run")
        for n in cp.notes:
            msgs.append(n)

        first_bad: int | None = None
        for c in cells:
            if c.position > cp.cell_position:
                break
            if cp.cell_versions.get(c.id) != c.current_version_id:
                msgs.append(f"cell {c.id} (position {c.position}) was edited or added since the checkpoint")
                first_bad = c.position
                break
            if any(n in bad_names or n not in restored for n in c.declared_outputs):
                missing = [n for n in c.declared_outputs if n in bad_names or n not in restored]
                msgs.append(f"cell {c.id} (position {c.position}) outputs not restored: {missing}")
                first_bad = c.position
                break
            if not c.declared_outputs:
                msgs.append(f"cell {c.id} (position {c.position}) declares no outputs; its in-memory effects were not checkpointed")
        stale: list[str] = []
        with self._uow() as u:
            for c in u.cells.list_cells(cp.paper_env_id):
                if c.status == CellStatus.PENDING:
                    continue
                if c.position <= cp.cell_position and (first_bad is None or c.position < first_bad):
                    c.status = CellStatus.SUCCESS if c.status != CellStatus.FAILED else c.status
                else:
                    c.status = CellStatus.STALE
                    stale.append(c.id)
                u.cells.update_cell(c)
        res = ResumeResult(cp.id, session.id, sorted(restored), not_restored, report["rng_restored"], stale,
                           cp.cell_position, msgs, prefix_ok=first_bad is None)
        self.bus.publish(EventType.CHECKPOINT_RESTORED, {
            "checkpoint_id": cp.id, "restored": res.restored, "not_restored": not_restored,
            "rng_restored": res.rng_restored, "stale_cells": stale, "messages": msgs, "complete": res.complete},
            paper_env_id=cp.paper_env_id)
        return res
