"""Run orchestration: creates Run records, executes cell plans, derives run status.

Same notebook version run twice => two Run rows; nothing is ever overwritten.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..backends import ComputeBackend
from ..config import Settings
from ..domain import (
    ActionKind, BlockedAction, Cell, CellRun, CellStatus, EventType, FailureKind, Run, RunMode, RunStatus, new_id, utcnow,
)
from ..env import EnvManager
from ..events import EventBus
from ..hooks import ActionGate
from ..kernel.runner import KernelRunner, Session, SessionError
from ..repos import UnitOfWorkFactory
from .notebook import NotebookService


class EnvNotReady(RuntimeError):
    pass


class RunInProgress(RuntimeError):
    pass


@dataclass
class _Plan:
    cell_ids: list[str]
    next_index: int
    stop_on_fail: bool
    cell_timeout: float | None
    deadline: float  # monotonic
    run_timeout: float = 0.0
    checkpoint: bool = False
    crs: list[CellRun] = field(default_factory=list)


class RunService:
    def __init__(self, settings: Settings, uow_factory: UnitOfWorkFactory, bus: EventBus, gate: ActionGate,
                 runner: KernelRunner, notebook: NotebookService, envs: EnvManager, backend: ComputeBackend,
                 datasets: Any | None = None):
        self.settings, self._uow, self.bus, self.gate = settings, uow_factory, bus, gate
        self.runner, self.notebook, self.envs, self.backend, self.datasets = runner, notebook, envs, backend, datasets
        self._plans: dict[str, _Plan] = {}
        self._active: dict[str, str] = {}  # paper_env_id -> run_id (one run per kernel at a time)
        self._active_lock = threading.Lock()
        self.checkpoints: Any | None = None  # CheckpointService, attached by the composition root
        # blocked cells resume through the run so the rest of the plan continues
        gate.register_executor(ActionKind.EXECUTE_CELL, self._resume_blocked_cell)
        gate.register_on_deny(ActionKind.EXECUTE_CELL, self._deny_blocked_cell)

    # ------------------------------------------------------------------ queries
    def get_run(self, run_id: str) -> Run:
        with self._uow() as u:
            return u.runs.get_run(run_id)

    def list_runs(self, paper_env_id: str) -> list[Run]:
        with self._uow() as u:
            return u.runs.list_runs(paper_env_id)

    def cell_runs(self, run_id: str) -> list[CellRun]:
        with self._uow() as u:
            return u.runs.list_cell_runs(run_id)

    # ------------------------------------------------------------------ public modes
    def run_cell(self, cell_id: str, *, seed: int | None = None, timeout: float | None = None,
                 run_id: str | None = None) -> Run:
        """Run one cell in the live kernel (starting one if needed). Downstream cells become stale."""
        cell = self.notebook.get_cell(cell_id)
        run = self._begin(cell.paper_env_id, RunMode.CELL, seed, fresh_kernel=False, run_id=run_id)
        if run.status != RunStatus.RUNNING:
            return run
        run = self._execute_plan(run, [cell_id], stop_on_fail=True, cell_timeout=timeout)
        self.notebook.mark_downstream_stale(cell_id)
        return run

    def run_until(self, cell_id: str, *, seed: int | None = None, cell_timeout: float | None = None,
                  run_timeout: float | None = None, stop_on_fail: bool = True, checkpoint: bool | None = None,
                  run_id: str | None = None) -> Run:
        """Top to bottom in the live kernel, up to and including ``cell_id``. Later cells become stale."""
        target = self.notebook.get_cell(cell_id)
        ids = [c.id for c in self.notebook.list_cells(target.paper_env_id) if c.position <= target.position]
        run = self._begin(target.paper_env_id, RunMode.UNTIL, seed, fresh_kernel=False, run_timeout=run_timeout, run_id=run_id)
        if run.status != RunStatus.RUNNING:
            return run
        run = self._execute_plan(run, ids, stop_on_fail=stop_on_fail, cell_timeout=cell_timeout, run_timeout=run_timeout,
                                  checkpoint=checkpoint)
        self.notebook.mark_downstream_stale(cell_id)
        return run

    def run_all(self, paper_env_id: str, *, stop_on_fail: bool = True, seed: int | None = None,
                cell_timeout: float | None = None, run_timeout: float | None = None,
                checkpoint: bool | None = None, run_id: str | None = None) -> Run:
        """Every cell in order in the live kernel. With ``stop_on_fail`` the rest become ``skipped``."""
        run = self._begin(paper_env_id, RunMode.ALL, seed, fresh_kernel=False, run_timeout=run_timeout, run_id=run_id)
        if run.status != RunStatus.RUNNING:
            return run
        ids = [c.id for c in self.notebook.list_cells(paper_env_id)]
        return self._execute_plan(run, ids, stop_on_fail=stop_on_fail, cell_timeout=cell_timeout, run_timeout=run_timeout,
                                  checkpoint=checkpoint)

    def run_clean(self, paper_env_id: str, *, stop_on_fail: bool = True, seed: int | None = None,
                  cell_timeout: float | None = None, run_timeout: float | None = None,
                  checkpoint: bool | None = None, run_id: str | None = None) -> Run:
        """Always a brand-new kernel, fixed seed, top to bottom. The ONLY mode that can be
        ``verification_eligible`` (and only if it ran to a real conclusion)."""
        run = self._begin(paper_env_id, RunMode.CLEAN, seed, fresh_kernel=True, verification_eligible=True, run_id=run_id)
        if run.status != RunStatus.RUNNING:
            return run
        ids = [c.id for c in self.notebook.list_cells(paper_env_id)]
        return self._execute_plan(run, ids, stop_on_fail=stop_on_fail, cell_timeout=cell_timeout, run_timeout=run_timeout,
                                  checkpoint=checkpoint)

    def continue_from(self, checkpoint_id: str, *, stop_on_fail: bool = True, cell_timeout: float | None = None,
                      run_timeout: float | None = None, checkpoint: bool | None = None):
        """Restore a checkpoint into a fresh kernel, then run the remaining cells as a new Run. Returns
        (ResumeResult, Run). Not verification-eligible: it did not start from a clean kernel."""
        if self.checkpoints is None:
            raise RuntimeError("checkpoint service is not configured")
        cp = self.checkpoints.get(checkpoint_id)
        res = self.checkpoints.resume_from(checkpoint_id)
        ids = [c.id for c in self.notebook.list_cells(cp.paper_env_id) if c.position > cp.cell_position]
        run = self._begin(cp.paper_env_id, RunMode.ALL, cp.seed, fresh_kernel=False)
        if run.status != RunStatus.RUNNING:
            return res, run
        run = self._execute_plan(run, ids, stop_on_fail=stop_on_fail, cell_timeout=cell_timeout,
                                 run_timeout=run_timeout, checkpoint=checkpoint)
        return res, run

    def interrupt(self, run_id: str) -> bool:
        """Interrupt whatever the run is executing; the run ends ``cancelled`` and the rest is skipped."""
        run = self.get_run(run_id)
        return self.runner.interrupt(run.paper_env_id)

    # ------------------------------------------------------------------ run lifecycle
    def _begin(self, paper_env_id: str, mode: RunMode, seed: int | None, *, fresh_kernel: bool,
               verification_eligible: bool = False, run_timeout: float | None = None,
               run_id: str | None = None) -> Run:
        rid = run_id or new_id()
        with self._active_lock:
            if paper_env_id in self._active:
                raise RunInProgress(f"run {self._active[paper_env_id]} is still active for this paper environment")
            self._active[paper_env_id] = rid
        try:
            return self._begin_locked(paper_env_id, mode, seed, fresh_kernel, verification_eligible, rid)
        except BaseException:
            self._release(paper_env_id, rid)
            raise

    def _release(self, paper_env_id: str, run_id: str) -> None:
        with self._active_lock:
            if self._active.get(paper_env_id) == run_id:
                del self._active[paper_env_id]

    def active_run_id(self, paper_env_id: str) -> str | None:
        return self._active.get(paper_env_id)

    def _begin_locked(self, paper_env_id: str, mode: RunMode, seed: int | None, fresh_kernel: bool,
                      verification_eligible: bool, rid: str) -> Run:
        with self._uow() as u:
            env = u.paper_envs.get(paper_env_id)
            spec = u.paper_envs.get_spec(env.env_spec_id)
        if not spec.lockfile_hash:
            raise EnvNotReady(f"environment for {env.alias!r} has no lockfile yet; build it with create_env first")
        seed = spec.seed if seed is None else seed
        run = Run(id=rid, paper_env_id=paper_env_id, notebook_version=env.current_notebook_version,
                  env_lockfile_hash=spec.lockfile_hash, mode=mode, seed=seed, backend=self.backend.capabilities().get("backend", "?"),
                  started_at=utcnow(), status=RunStatus.RUNNING, verification_eligible=verification_eligible)
        report = None
        if self.datasets is not None and env.dataset_ids:
            report = self.datasets.preflight(env.dataset_ids)
            run.dataset_manifests, run.approximate_split = report.dataset_manifests, report.approximate_split
        with self._uow() as u:
            u.runs.add_run(run)
        if report is not None and not report.ok:
            # fail before any cell executes, with the reason spelled out
            return self._finish(run, RunStatus.INFRA_FAILED, FailureKind.INFRASTRUCTURE,
                                f"dataset preflight failed: {report.message}",
                                extra={"preflight_problems": [p.__dict__ for p in report.problems]})
        try:
            if fresh_kernel:
                session = self.runner.start_session(paper_env_id, seed, **self._kernel_opts(run.id, env))
            else:
                session = self.runner.ensure_session(paper_env_id, seed, **self._kernel_opts(run.id, env))
        except SessionError as e:
            return self._finish(run, RunStatus.INFRA_FAILED, FailureKind.INFRASTRUCTURE, f"could not start kernel: {e}")
        run.kernel_session_id = session.id
        try:
            hw = self.envs.hardware_info(paper_env_id)
        except FileNotFoundError:
            hw = {"error": "environment interpreter not found"}
        hw["seeded"] = session.handle.info.get("seeded")
        hw["lockfile_hash"] = spec.lockfile_hash
        run.hardware_info = hw
        with self._uow() as u:
            u.runs.update_run(run)
        if self.datasets is not None:
            self.datasets.acquire(run.id, env.dataset_ids)
        self.bus.publish(EventType.RUN_STARTED, {
            "run_id": run.id, "mode": mode.value, "seed": seed, "notebook_version": run.notebook_version,
            "lockfile_hash": run.env_lockfile_hash, "verification_eligible": verification_eligible,
            "approximate_split": run.approximate_split}, paper_env_id=paper_env_id, run_id=run.id)
        return run

    def _kernel_opts(self, run_id: str, env) -> dict[str, Any]:
        opts: dict[str, Any] = {"workdir": self.settings.files_dir(env.id)}
        if self.datasets is not None:
            ids = list(env.dataset_ids) + self._derived_ids(env.id)
            manifest = self.datasets.prepare_mount(env.id, run_id, ids)
            opts["extra_env"] = {"PAPER_ENV_DATASETS_MANIFEST": str(manifest)}
        return opts

    def _derived_ids(self, paper_env_id: str) -> list[str]:
        """Derived datasets produced by earlier runs of this paper env stay visible to later runs."""
        with self._uow() as u:
            run_ids = {r.id for r in u.runs.list_runs(paper_env_id)}
            return [m.dataset_id for m in u.datasets.list() if m.run_id in run_ids]

    def _finish(self, run: Run, status: RunStatus, kind: FailureKind | None, message: str | None = None,
                extra: dict[str, Any] | None = None) -> Run:
        run.status, run.failure_kind, run.message = status, kind, message
        run.finished_at = utcnow()
        if status in (RunStatus.INFRA_FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.BLOCKED):
            run.verification_eligible = False  # incomplete runs cannot back a verification
        with self._uow() as u:
            u.runs.update_run(run)
        if self.datasets is not None:
            self.datasets.release(run.id)
        self._release(run.paper_env_id, run.id)
        self.bus.publish(EventType.RUN_FINISHED, {
            "run_id": run.id, "status": status.value, "failure_kind": kind.value if kind else None,
            "message": message, **(extra or {})}, paper_env_id=run.paper_env_id, run_id=run.id)
        return run

    # ------------------------------------------------------------------ plan execution
    def _skip(self, run: Run, cell_id: str, reason: str) -> CellRun:
        with self._uow() as u:
            c = u.cells.get_cell(cell_id)
            cr = CellRun(run_id=run.id, cell_id=cell_id, cell_version_id=c.current_version_id,
                         status=CellStatus.SKIPPED, outputs=[{"type": "note", "text": reason}])
            u.runs.add_cell_run(cr)
            c.status = CellStatus.SKIPPED
            u.cells.update_cell(c)
        return cr

    def _execute_plan(self, run: Run, cell_ids: list[str], **kw: Any) -> Run:
        try:
            return self._execute_plan_inner(run, cell_ids, **kw)
        except Exception as e:  # never leave the env locked or the run 'running' forever
            self._plans.pop(run.id, None)
            if run.finished_at is None:
                self._finish(run, RunStatus.INFRA_FAILED, FailureKind.INFRASTRUCTURE,
                             f"internal error: {type(e).__name__}: {self.bus.scrubber.scrub(str(e))[:300]}")
            raise

    def _execute_plan_inner(self, run: Run, cell_ids: list[str], *, stop_on_fail: bool, cell_timeout: float | None,
                            run_timeout: float | None = None, checkpoint: bool | None = None,
                            plan: _Plan | None = None) -> Run:
        total = run_timeout if run_timeout is not None else self.settings.run_timeout_s
        plan = plan or _Plan(cell_ids, 0, stop_on_fail, cell_timeout, time.monotonic() + total, total,
                             self.settings.checkpoint_after_each_cell if checkpoint is None else checkpoint)
        while plan.next_index < len(plan.cell_ids):
            cid = plan.cell_ids[plan.next_index]
            remaining = plan.deadline - time.monotonic()
            if remaining <= 0:
                return self._abort_rest(run, plan, RunStatus.TIMED_OUT, None,
                                        f"run exceeded its {plan.run_timeout:g}s total runtime limit")
            wanted = plan.cell_timeout or self.settings.cell_timeout_s
            timeout = min(wanted, remaining)
            clipped = remaining < wanted  # the run's total budget, not the cell's own limit, set this timeout
            cr = self.runner.execute_cell(run, cid, timeout=timeout)
            plan.next_index += 1
            plan.crs.append(cr)
            if plan.checkpoint and cr.status == CellStatus.SUCCESS and self.checkpoints is not None:
                self.checkpoints.save(run, cr)
            if cr.status == CellStatus.BLOCKED:
                self._plans[run.id] = plan
                run.status = RunStatus.BLOCKED
                with self._uow() as u:
                    u.runs.update_run(run)
                return run
            if cr.status == CellStatus.FAILED:
                if cr.failure_kind == FailureKind.INFRASTRUCTURE:
                    return self._abort_rest(run, plan, RunStatus.INFRA_FAILED, FailureKind.INFRASTRUCTURE,
                                            f"infrastructure failure: {cr.error.message}")
                if clipped and cr.error and cr.error.type == "CellTimeout":
                    return self._abort_rest(run, plan, RunStatus.TIMED_OUT, None,
                                            f"run exceeded its {plan.run_timeout:g}s total runtime limit during cell {cid}")
                if cr.error and cr.error.type == "Interrupted":
                    return self._abort_rest(run, plan, RunStatus.CANCELLED, None, "interrupted by user")
                if plan.stop_on_fail:
                    return self._abort_rest(run, plan, RunStatus.FAILED, FailureKind.EXPERIMENT,
                                            f"cell {cid} failed: {cr.error.type if cr.error else 'error'}", cause=cid)
        failed = [c for c in plan.crs if c.status == CellStatus.FAILED]
        if failed:
            return self._finish(run, RunStatus.FAILED, FailureKind.EXPERIMENT, f"{len(failed)} cell(s) failed")
        return self._finish(run, RunStatus.SUCCESS, None)

    def _abort_rest(self, run: Run, plan: _Plan, status: RunStatus, kind: FailureKind | None, message: str,
                    cause: str | None = None) -> Run:
        """Remaining cells are SKIPPED (not failed) and the run finishes with ``status``."""
        reason = {RunStatus.INFRA_FAILED: "skipped: the kernel was lost (infrastructure failure)",
                  RunStatus.CANCELLED: "skipped: run interrupted",
                  RunStatus.TIMED_OUT: "skipped: run exceeded its total runtime limit"}.get(
            status, f"skipped: earlier cell {cause} failed (stop_on_fail)")
        for cid in plan.cell_ids[plan.next_index:]:
            plan.crs.append(self._skip(run, cid, reason))
        plan.next_index = len(plan.cell_ids)
        self._plans.pop(run.id, None)
        return self._finish(run, status, kind, message)

    # ------------------------------------------------------------------ blocked actions
    def _deny_blocked_cell(self, blocked: BlockedAction) -> Run:
        """Final denial: the paused cell and everything after it is skipped; the run is cancelled."""
        run_id = blocked.payload["run_id"]
        plan = self._plans.pop(run_id, None)
        run = self.get_run(run_id)
        with self._uow() as u:
            crs = u.runs.list_cell_runs(run_id)
            cr = next(c for c in crs if c.id == blocked.payload["cell_run_id"])
            cr.status, cr.blocked_action_id = CellStatus.SKIPPED, None
            cr.outputs = [{"type": "note", "text": f"skipped: execution denied ({blocked.reason})"}]
            u.runs.update_cell_run(cr)
            cell = u.cells.get_cell(blocked.payload["cell_id"])
            cell.status = CellStatus.SKIPPED
            u.cells.update_cell(cell)
        if plan is not None:
            return self._abort_rest(run, plan, RunStatus.CANCELLED, None, f"execution denied: {blocked.reason}")
        return self._finish(run, RunStatus.CANCELLED, None, f"execution denied: {blocked.reason}")

    def _resume_blocked_cell(self, blocked: BlockedAction) -> Run:
        """Approved cell: run it, then continue the rest of the paused plan."""
        run_id = blocked.payload["run_id"]
        plan = self._plans.pop(run_id, None)
        run = self.get_run(run_id)
        run.status = RunStatus.RUNNING
        with self._uow() as u:
            u.runs.update_run(run)
        cr = self.runner._execute_blocked(blocked)
        if plan is None:  # e.g. server restarted: the plan is gone, so finish honestly
            ok = cr.status == CellStatus.SUCCESS
            kind = None if ok else (cr.failure_kind or FailureKind.EXPERIMENT)
            return self._finish(run, RunStatus.SUCCESS if ok else RunStatus.FAILED, kind,
                                None if ok else "resumed cell failed; remaining plan was not retained")
        plan.crs[-1:] = [cr]
        if cr.status == CellStatus.FAILED:
            if cr.failure_kind == FailureKind.INFRASTRUCTURE:
                return self._abort_rest(run, plan, RunStatus.INFRA_FAILED, FailureKind.INFRASTRUCTURE, cr.error.message)
            if plan.stop_on_fail:
                return self._abort_rest(run, plan, RunStatus.FAILED, FailureKind.EXPERIMENT, "resumed cell failed", cr.cell_id)
        return self._execute_plan(run, plan.cell_ids, stop_on_fail=plan.stop_on_fail, cell_timeout=plan.cell_timeout, run_timeout=plan.run_timeout,
                                  checkpoint=plan.checkpoint, plan=plan)
