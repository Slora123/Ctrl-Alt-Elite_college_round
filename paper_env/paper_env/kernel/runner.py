"""Kernel runner: executes cells through a ComputeBackend and records what happened.

Experiment failure (the cell raised) and infrastructure failure (kernel died / OOM / backend)
are kept distinct in CellRun.failure_kind, Cell.last_failure_kind, Run.failure_kind and in the
event stream (``cell_failed`` vs ``kernel_died``).
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..backends.registry import RuntimeUnavailable, as_registry
from ..backends import (
    BackendError, ComputeBackend, ExecDisplay, ExecError, ExecFinished, ExecInfraFailure, ExecOutput,
    RuntimeHandle,
)
from ..backends.local import INTERNAL_MIME
from ..config import Settings
from ..domain import (
    ActionKind, BlockedAction, Cell, CellRun, CellStatus, ErrorInfo, EventType, FailureKind, Run, utcnow,
)
from ..backends import ExecInputRequest
from ..events import EventBus
from ..hooks import Action, ActionBlocked, ActionGate
from ..repos import UnitOfWorkFactory
from .outputs import OutputRecorder, strip_ansi
from ..services.notebook import NotebookService

_CELL_FRAME = re.compile(r"(?:Cell In\[\d+\]|Input In \[\d+\]|File \"<ipython-input-[^\"]+>\"|File \"<string>\"),? line (\d+)")


class SessionError(RuntimeError):
    pass


@dataclass
class Session:
    """A live kernel plus the facts that define its in-memory state."""
    id: str
    paper_env_id: str
    handle: RuntimeHandle
    seed: int
    started_at: datetime = field(default_factory=utcnow)
    alive: bool = True
    death: dict[str, Any] | None = None  # why it died, if it did
    lock: threading.Lock = field(default_factory=threading.Lock)
    backend: Any = None  # the ComputeBackend this session lives on (chosen by the PaperEnv's runtime profile)
    profile_id: str = "local-cpu"
    cost_per_hr: float = 0.0
    started_mono: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    busy: bool = False

    def touch(self) -> None:
        self.last_activity = time.monotonic()


def parse_traceback(source: str, ename: str, evalue: str, tb: list[str]) -> ErrorInfo:
    """Extract type/message/traceback/failing line. Traceback text is untrusted: it is only read, never run."""
    lines = [strip_ansi(x) for x in tb]
    text = "\n".join(lines)
    lineno = None
    for m in _CELL_FRAME.finditer(text):
        lineno = int(m.group(1))  # last match = innermost frame inside the cell
    src_lines = source.splitlines()
    failing = src_lines[lineno - 1].strip() if lineno and 1 <= lineno <= len(src_lines) else None
    return ErrorInfo(type=ename, message=evalue, traceback=text, failing_line=failing, failing_lineno=lineno)


class KernelRunner:
    def __init__(self, settings: Settings, uow_factory: UnitOfWorkFactory, bus: EventBus, gate: ActionGate,
                 backend: ComputeBackend, notebook: NotebookService, datasets: Any | None = None):
        self.settings, self._uow, self.bus, self.gate = settings, uow_factory, bus, gate
        self.registry = as_registry(backend)
        self.backend = self.registry.default  # fallback for code that has no session yet
        self.notebook, self.datasets = notebook, datasets
        self.sessions: dict[str, Session] = {}
        self._guard = threading.Lock()
        gate.register_executor(ActionKind.EXECUTE_CELL, self._execute_blocked)

    # ------------------------------------------------------------------ sessions
    def get_session(self, paper_env_id: str) -> Session | None:
        return self.sessions.get(paper_env_id)

    def start_session(self, paper_env_id: str, seed: int, **provision_opts: Any) -> Session:
        """Fresh kernel (replacing any existing one). Raises SessionError if the backend cannot provide one."""
        self.shutdown(paper_env_id)
        with self._uow() as u:
            env = u.paper_envs.get(paper_env_id)
            spec = u.paper_envs.get_spec(env.env_spec_id)
        try:
            profile = self.registry.profile(env.runtime)
            backend = self.registry.backend_for(env.runtime)
            handle = backend.provision(spec, paper_env_id=paper_env_id, seed=seed, **provision_opts)
        except (BackendError, RuntimeUnavailable) as e:
            raise SessionError(str(e)) from e
        s = Session(id=uuid.uuid4().hex[:12], paper_env_id=paper_env_id, handle=handle, seed=seed, backend=backend,
                    profile_id=profile.id, cost_per_hr=profile.cost_per_hr)
        backend.on_comm(handle, lambda ev, pid=paper_env_id: self.bus.emit_ephemeral(
            EventType.COMM_MESSAGE, {"msg_type": ev.msg_type, "content": ev.content, "buffers": ev.buffers,
                                     "metadata": ev.metadata}, paper_env_id=pid))
        with self._guard:
            self.sessions[paper_env_id] = s
        return s

    def ensure_session(self, paper_env_id: str, seed: int, **opts: Any) -> Session:
        s = self.sessions.get(paper_env_id)
        if s and s.alive:
            return s
        return self.start_session(paper_env_id, seed, **opts)

    def restart_kernel(self, paper_env_id: str, seed: int | None = None, **opts: Any) -> Session:
        """New kernel, same seed unless told otherwise. Everything that ran is now stale: the old
        in-memory state is gone and we do not pretend otherwise."""
        old = self.sessions.get(paper_env_id)
        seed = old.seed if (old and seed is None) else (seed or 0)
        self.shutdown(paper_env_id)
        s = self.start_session(paper_env_id, seed, **opts)
        stale = self.notebook.mark_all_stale(paper_env_id)
        self.bus.publish(EventType.ENV_CHANGED, {
            "op": "kernel_restart", "session_id": s.id, "stale_cells": stale,
            "note": "kernel restarted; all executed cells are stale"}, paper_env_id=paper_env_id)
        return s

    def _live(self, paper_env_id: str) -> Session:
        s = self.sessions.get(paper_env_id)
        if s is None or not s.alive:
            raise SessionError("no live kernel; run a cell first")
        return s

    def provide_input(self, paper_env_id: str, text: str) -> None:
        s = self._live(paper_env_id)
        s.touch()
        s.backend.provide_input(s.handle, text)

    def complete(self, paper_env_id: str, code: str, cursor_pos: int) -> dict[str, Any]:
        s = self._live(paper_env_id)
        s.touch()
        return s.backend.complete(s.handle, code, cursor_pos)

    def inspect(self, paper_env_id: str, code: str, cursor_pos: int) -> dict[str, Any]:
        s = self._live(paper_env_id)
        s.touch()
        return s.backend.inspect(s.handle, code, cursor_pos)

    def comm_send(self, paper_env_id: str, msg_type: str, content: dict[str, Any], buffers: list[str] | None = None) -> None:
        s = self._live(paper_env_id)
        s.touch()
        s.backend.comm_send(s.handle, msg_type, content, buffers)

    def interrupt(self, paper_env_id: str) -> bool:
        s = self.sessions.get(paper_env_id)
        if not s or not s.alive:
            return False
        s.backend.interrupt(s.handle)
        return True

    def shutdown(self, paper_env_id: str | None = None) -> None:
        ids = [paper_env_id] if paper_env_id else list(self.sessions)
        for pid in ids:
            with self._guard:
                s = self.sessions.pop(pid, None)
            (self.settings.env_dir(pid) / "userdata.json").unlink(missing_ok=True)  # granted secrets leave with the kernel
            if s:
                s.backend.teardown(s.handle)

    def expire(self, paper_env_id: str, reason: str, message: str) -> bool:
        """End a session because of a limit (idle / max age / cost) or a runtime change. Whatever was in memory is
        gone, so every executed cell becomes stale and a ``session_expired`` event says so."""
        s = self.sessions.get(paper_env_id)
        if s is None:
            return False
        if s.busy:
            s.backend.interrupt(s.handle)
            end = time.monotonic() + 10
            while s.busy and time.monotonic() < end:
                time.sleep(0.1)
        self.shutdown(paper_env_id)
        stale = self.notebook.mark_all_stale(paper_env_id)
        self.bus.publish(EventType.SESSION_EXPIRED, {"reason": reason, "message": message, "state_lost": True,
                                                     "stale_cells": stale}, paper_env_id=paper_env_id)
        return True

    def session_info(self, paper_env_id: str) -> dict[str, Any] | None:
        s = self.sessions.get(paper_env_id)
        if s is None:
            return None
        now = time.monotonic()
        age = now - s.started_mono
        return {"session_id": s.id, "alive": s.alive, "busy": s.busy, "runtime": s.profile_id, "age_s": round(age, 1),
                "idle_s": 0.0 if s.busy else round(now - s.last_activity, 1), "cost_so_far": round(age / 3600 * s.cost_per_hr, 4),
                "cost_per_hr": s.cost_per_hr}

    # ------------------------------------------------------------------ execution
    def execute_cell(self, run: Run, cell_id: str, *, timeout: float | None = None) -> CellRun:
        """Run one cell inside ``run``. Always returns a CellRun describing the outcome (never raises for
        experiment/infra failures)."""
        with self._uow() as u:
            cell = u.cells.get_cell(cell_id)
            ver = u.cells.get_version(cell.current_version_id)
        cr = CellRun(run_id=run.id, cell_id=cell_id, cell_version_id=ver.id, status=CellStatus.PENDING)
        try:
            self.gate.check(Action(ActionKind.EXECUTE_CELL, paper_env_id=run.paper_env_id, run_id=run.id, payload={
                "cell_id": cell_id, "cell_version_id": ver.id, "run_id": run.id, "source": ver.source[:10000],
                "cell_run_id": cr.id, "timeout": timeout}))
        except ActionBlocked as b:
            cr.status, cr.blocked_action_id = CellStatus.BLOCKED, b.action_id
            with self._uow() as u:
                u.runs.add_cell_run(cr)
                cell.status = CellStatus.BLOCKED
                u.cells.update_cell(cell)
            return cr
        with self._uow() as u:
            u.runs.add_cell_run(cr)
        return self._execute(run, cell, ver.source, cr, timeout)

    def _execute_blocked(self, blocked: BlockedAction) -> CellRun:
        """Executor invoked by ``resume_blocked(..., allow)``: run the paused cell in its original run."""
        p = blocked.payload
        with self._uow() as u:
            run = u.runs.get_run(p["run_id"])
            cell = u.cells.get_cell(p["cell_id"])
            ver = u.cells.get_version(p["cell_version_id"])
            cr = next(c for c in u.runs.list_cell_runs(run.id) if c.id == p["cell_run_id"])
        return self._execute(run, cell, ver.source, cr, p.get("timeout"))

    def _execute(self, run: Run, cell: Cell, source: str, cr: CellRun, timeout: float | None) -> CellRun:
        session = self.sessions.get(run.paper_env_id)
        timeout = timeout if timeout is not None else self.settings.cell_timeout_s
        cr.blocked_action_id = None
        cr.status = CellStatus.RUNNING
        self._save(cr, cell, CellStatus.RUNNING)
        self.bus.publish(EventType.CELL_STARTED, {
            "cell_id": cell.id, "cell_version_id": cr.cell_version_id, "position": cell.position,
            "cell_run_id": cr.id, "timeout": timeout}, paper_env_id=run.paper_env_id, run_id=run.id)
        if session is None or not session.alive:
            return self._infra_fail(run, cell, cr, "backend_error", (
                "no live kernel session (it died or was never started); in-memory state is lost - "
                "restart the kernel"), None, started=time.monotonic())
        rec = OutputRecorder(self.bus.scrubber, self.settings.artifacts_dir(run.paper_env_id) / run.id / f"{cr.id}.jsonl",
                             self.settings.max_output_bytes_db, self.settings.max_output_bytes_total)
        t0 = time.monotonic()
        error: ErrorInfo | None = None
        final = None
        infra: ExecInfraFailure | None = None
        interrupted_for_limit = False
        with session.lock:  # cells in one kernel run strictly one at a time
            session.busy = True
            session.touch()
            for ev in session.backend.execute(session.handle, source, timeout):
                if isinstance(ev, ExecOutput):
                    for text, overwrite in rec.stream(ev.stream, ev.text):
                        self._emit_output(run, cell, cr, {"type": "stream", "name": ev.stream, "text": text,
                                                          "overwrite": overwrite})
                elif isinstance(ev, ExecDisplay):
                    if INTERNAL_MIME in ev.data or rec.limit_exceeded:
                        continue
                    self._emit_output(run, cell, cr, {"type": "display", "data": rec.display(ev.data, ev.metadata, ev.is_result)})
                elif isinstance(ev, ExecError):
                    error = parse_traceback(source, ev.ename, ev.evalue, ev.traceback)
                elif isinstance(ev, ExecFinished):
                    final = ev.status
                elif isinstance(ev, ExecInputRequest):
                    self.bus.publish(EventType.INPUT_REQUESTED, {
                        "cell_id": cell.id, "cell_run_id": cr.id, "prompt": ev.prompt, "password": ev.password},
                        paper_env_id=run.paper_env_id, run_id=run.id)
                elif isinstance(ev, ExecInfraFailure):
                    infra = ev
                if rec.limit_exceeded and not interrupted_for_limit and final is None:
                    interrupted_for_limit = True  # once: repeated SIGINTs would also hit the next cell
                    session.backend.interrupt(session.handle)
            for name, text in rec.flush():
                self._emit_output(run, cell, cr, {"type": "stream", "name": name, "text": text})
            duration = time.monotonic() - t0
            try:
                if infra is None and final == "ok" and not rec.limit_exceeded:
                    cr.state_hash = self._state_hash(session, cell, rec)
            finally:
                session.busy = False
                session.touch()
        cr.outputs, cr.output_truncated, cr.full_output_path = rec.finish()
        cr.duration = duration

        if infra is not None:
            return self._infra_fail(run, cell, cr, infra.kind, infra.message, infra.exit_code, session=session)
        if rec.limit_exceeded:
            error = ErrorInfo(type="OutputLimitExceeded", message=(
                f"cell produced more than {self.settings.max_output_bytes_total} bytes of output and was interrupted"))
            return self._experiment_fail(run, cell, cr, error)
        if final == "timeout":
            error = ErrorInfo(type="CellTimeout", message=f"cell exceeded its {timeout:g}s timeout and was interrupted")
            return self._experiment_fail(run, cell, cr, error)
        if final == "interrupted":
            cr.status, cr.failure_kind = CellStatus.FAILED, None
            cr.error = ErrorInfo(type="Interrupted", message="interrupted by user")
            self._save(cr, cell, CellStatus.FAILED, None)
            self.bus.publish(EventType.CELL_FAILED, self._failed_payload(cell, cr, interrupted=True),
                             paper_env_id=run.paper_env_id, run_id=run.id)
            return cr
        if final == "error" or error is not None:
            return self._experiment_fail(run, cell, cr, error or ErrorInfo(type="Error", message="cell failed"))
        cr.status = CellStatus.SUCCESS
        derived = self._register_derived(run, cell, cr)
        self._save(cr, cell, CellStatus.SUCCESS, None)
        self.bus.publish(EventType.CELL_FINISHED, {
            "derived_datasets": derived,
            "cell_id": cell.id, "cell_version_id": cr.cell_version_id, "cell_run_id": cr.id, "status": "success",
            "duration": cr.duration, "state_hash": cr.state_hash, "output_truncated": cr.output_truncated,
            "full_output_path": cr.full_output_path}, paper_env_id=run.paper_env_id, run_id=run.id)
        return cr

    def _register_derived(self, run: Run, cell: Cell, cr: CellRun) -> list[str]:
        """Datasets the cell created through ``datasets.derive`` become registered, linked derived data."""
        if self.datasets is None:
            return []
        with self._uow() as u:
            author = u.cells.get_version(cr.cell_version_id).author.value
        made, errors = self.datasets.collect_derived(run.paper_env_id, run.id, cr.cell_version_id, author)
        for e in errors:
            cr.outputs.append({"type": "note", "text": self.bus.scrubber.scrub(e)})
        return [m.dataset_id for m in made]

    # ------------------------------------------------------------------ outcomes
    def _failed_payload(self, cell: Cell, cr: CellRun, **extra: Any) -> dict[str, Any]:
        e = cr.error
        return {"cell_id": cell.id, "cell_version_id": cr.cell_version_id, "cell_run_id": cr.id,
                "exception_type": e.type, "message": e.message, "traceback": e.traceback,
                "failing_line": e.failing_line, "failing_lineno": e.failing_lineno,
                "failure_kind": cr.failure_kind.value if cr.failure_kind else None, **extra}

    def _experiment_fail(self, run: Run, cell: Cell, cr: CellRun, error: ErrorInfo) -> CellRun:
        s = self.bus.scrubber
        error.message, error.traceback = s.scrub(error.message), s.scrub(error.traceback)
        error.failing_line = s.scrub(error.failing_line) if error.failing_line else None
        cr.status, cr.error, cr.failure_kind = CellStatus.FAILED, error, FailureKind.EXPERIMENT
        self._save(cr, cell, CellStatus.FAILED, FailureKind.EXPERIMENT)
        self.bus.publish(EventType.CELL_FAILED, self._failed_payload(cell, cr), paper_env_id=run.paper_env_id, run_id=run.id)
        return cr

    def _infra_fail(self, run: Run, cell: Cell, cr: CellRun, kind: str, message: str, exit_code: int | None,
                    session: Session | None = None, started: float | None = None) -> CellRun:
        message = self.bus.scrubber.scrub(message)
        cr.status, cr.failure_kind = CellStatus.FAILED, FailureKind.INFRASTRUCTURE
        cr.error = ErrorInfo(type=kind, message=message)
        if started is not None:
            cr.duration = time.monotonic() - started
        if session is not None:
            session.alive, session.death = False, {"kind": kind, "message": message, "exit_code": exit_code}
        self._save(cr, cell, CellStatus.FAILED, FailureKind.INFRASTRUCTURE)
        # in-memory state is gone: everything else that ran can no longer claim its state exists
        stale = self.notebook.mark_all_stale(run.paper_env_id, except_ids={cell.id})
        self.bus.publish(EventType.KERNEL_DIED, {
            "kind": kind, "message": message, "exit_code": exit_code, "cell_id": cell.id,
            "cell_version_id": cr.cell_version_id, "cell_run_id": cr.id, "failure_kind": "infrastructure",
            "stale_cells": stale, "state_lost": True}, paper_env_id=run.paper_env_id, run_id=run.id)
        return cr

    # ------------------------------------------------------------------ helpers
    def _save(self, cr: CellRun, cell: Cell, status: CellStatus, kind: FailureKind | None | str = "keep") -> None:
        with self._uow() as u:
            u.runs.update_cell_run(cr)
            c = u.cells.get_cell(cell.id)
            c.status = status
            if kind != "keep":
                c.last_failure_kind = kind  # type: ignore[assignment]
            u.cells.update_cell(c)

    def _emit_output(self, run: Run, cell: Cell, cr: CellRun, out: dict[str, Any]) -> None:
        self.bus.publish(EventType.CELL_OUTPUT, {"cell_id": cell.id, "cell_run_id": cr.id, **out},
                         paper_env_id=run.paper_env_id, run_id=run.id)

    def run_internal(self, session: Session, code: str, timeout: float = 120.0) -> tuple[list[dict], ExecError | ExecInfraFailure | None]:
        """Run runtime-support code in the kernel; returns structured internal payloads (no eval, no text parsing)."""
        out, err = [], None
        for ev in session.backend.execute(session.handle, code, timeout, internal=True):
            if isinstance(ev, ExecDisplay) and INTERNAL_MIME in ev.data:
                out.append(ev.data[INTERNAL_MIME])
            elif isinstance(ev, (ExecError, ExecInfraFailure)):
                err = ev
        return out, err

    def _state_hash(self, session: Session, cell: Cell, rec: OutputRecorder) -> str | None:
        names = json.dumps(sorted(cell.declared_outputs))
        payloads, err = self.run_internal(session, f"_pe.state_hash(__import__('json').loads({names!r}))", 120.0)
        for p in payloads:
            if p.get("kind") == "state_hash":
                return p["state_hash"]
        rec.note(f"state hash unavailable: {getattr(err, 'evalue', None) or getattr(err, 'message', 'no result')}")
        return None
