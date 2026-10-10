"""Executor that runs clean-run jobs inside Paper Environment kernels instead of a bare subprocess.

What it adds over ``SubprocessNotebookExecutor``: a managed uv environment per requirement set (locked, hashed), a real
Jupyter kernel with seeded RNGs, per-cell and per-run timeouts, kernel-death / OOM classification as *infrastructure*
failures, output caps and secret scrubbing, and the lockfile hash as the result's environment identity.

It is still NOT a sandbox: the kernel runs as the worker's OS user with network and filesystem access. The worker keeps
advertising ``isolation="subprocess"`` so jobs that require strong isolation are never sent to it.
"""
from __future__ import annotations

import hashlib
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from crisisgrid_core.scrub import scrub

from .executor import ExecResult

MAGIC = re.compile(r"^\s*[%!]")
INSTALL = re.compile(r"\b(pip3?|uv|conda|mamba|apt(?:-get)?)\b[^\n]*\binstall\b")


class PaperEnvExecutor:
    def __init__(self, data_dir: str | Path, *, secrets: list[str] | None = None, python_version: str | None = None) -> None:
        from paper_env.config import Settings
        from paper_env.container import build_app

        self.secrets = secrets or []
        self.python_version = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
        self.core = build_app(Settings(data_dir=Path(data_dir).resolve(), reaper_interval_s=0))
        self.core.startup()
        self._envs: dict[str, str] = {}  # requirement-set hash -> paper_env_id
        self._lock = threading.Lock()  # a worker runs one job at a time; this also guards the env cache

    # ------------------------------------------------------------------ environment per requirement set
    def _env_for(self, requirements: list[str]) -> tuple[str | None, str | None]:
        from paper_env.domain import EnvSpec

        key = hashlib.sha256("\n".join(sorted(requirements)).encode() + self.python_version.encode()).hexdigest()[:16]
        if key in self._envs:
            return self._envs[key], None
        spec = EnvSpec(python_version=self.python_version, requirements=list(requirements))
        env = self.core.paper_envs.create(f"job-env-{key}", paper_hash=key, spec=spec)
        res = self.core.envs.create_env(env.id)
        if not res.success:
            detail = res.conflict_details or res.error or "unknown error"
            return None, f"could not build the job environment ({res.failing_package or 'n/a'}): {detail[:500]}"
        self._envs[key] = env.id
        return env.id, None

    # ------------------------------------------------------------------ run
    def run(self, payload: dict[str, Any], *, seed: int, timeout_s: int, on_log: Callable[[str], None]) -> ExecResult:
        from paper_env.domain import Author, CellStatus, EventType, FailureKind, RunStatus

        cells = payload.get("cells")
        if not isinstance(cells, list) or not cells:
            return ExecResult(status="infra_failed", error="job payload has no cells")
        code = [c for c in cells if c.get("kind", "code") == "code"]
        for i, cell in enumerate(code):  # same contract as the subprocess driver: environment changes are not cell business
            bad = next((ln for ln in cell["source"].splitlines() if MAGIC.match(ln) or INSTALL.search(ln)), None)
            if bad is not None:
                msg = f"cell {i} uses a magic or installs packages ({bad.strip()[:80]!r}); environment changes go through the Env manager"
                return ExecResult(status="failed", error=msg, result={"status": "failed", "metrics": {}, "cells_run": 0, "seed": seed,
                                                                        "error": {"type": "UnsupportedCell", "cell_index": i, "message": msg}})
        with self._lock:
            env_id, err = self._env_for(list(payload.get("requirements") or []))
            if env_id is None:
                return ExecResult(status="infra_failed", error=scrub(err or "environment build failed", self.secrets))
            core = self.core
            for old in core.notebook.list_cells(env_id):  # fresh notebook for every job
                core.notebook.delete_cell(old.id)
            for cell in code:
                core.notebook.add_cell(env_id, cell["source"], Author.GENERATOR)

            def forward(ev: Any) -> None:
                p = ev.payload
                if ev.type == EventType.CELL_OUTPUT and p.get("type") == "stream" and not p.get("overwrite"):
                    for line in str(p.get("text", "")).splitlines():
                        on_log(scrub(line, self.secrets))

            sub = core.bus.subscribe(forward, paper_env_id=env_id)
            try:
                run = core.runs.run_clean(env_id, seed=seed, cell_timeout=float(timeout_s), run_timeout=float(timeout_s),
                                          stop_on_fail=True)
                crs = core.runs.cell_runs(run.id)
                metrics: dict[str, float] = {}
                session = core.runner.get_session(env_id)
                if session is not None and session.alive:
                    payloads, _ = core.runner.run_internal(session, "_pe.dump_metrics()", 30.0)
                    for pl in payloads:
                        if pl.get("kind") == "metrics":
                            metrics = {k: float(v) for k, v in pl["metrics"].items()}
            finally:
                sub.close()
                core.runner.shutdown(env_id)  # clean runs never share a kernel
        hw = dict(run.hardware_info)
        wire: dict[str, Any] = {
            "status": "ok" if run.status == RunStatus.SUCCESS else "failed", "metrics": metrics, "error": None,
            "seeded": hw.get("seeded"), "cells_run": sum(1 for c in crs if c.status == CellStatus.SUCCESS),
            "hardware_info": {"python": (hw.get("env_python") or {}).get("python_version"), "platform": hw.get("platform"),
                              "cpu_count": hw.get("cpu_count"), "cuda": hw.get("cuda")},
            "lockfile_digest": run.env_lockfile_hash, "seed": seed,
            "duration_s": round(((run.finished_at or run.started_at) - run.started_at).total_seconds(), 3) if run.started_at else 0.0,
            "executor": "paper_env",
        }
        if run.status == RunStatus.SUCCESS:
            return ExecResult(status="succeeded", result=wire)
        failing = next((c for c in crs if c.status == CellStatus.FAILED), None)
        if failing is not None and failing.error is not None:
            idx = next((i for i, c in enumerate(crs) if c.id == failing.id), 0)
            wire["error"] = {"type": failing.error.type, "message": scrub(failing.error.message, self.secrets)[:2000],
                             "cell_index": idx, "traceback": scrub(failing.error.traceback, self.secrets)[-4000:]}
        msg = scrub(f"{failing.error.type}: {failing.error.message}" if failing and failing.error else (run.message or "run failed"), self.secrets)
        if run.status == RunStatus.TIMED_OUT:
            return ExecResult(status="timeout", error=f"exceeded the {timeout_s}s time limit", result=wire)
        if run.status == RunStatus.INFRA_FAILED or run.failure_kind == FailureKind.INFRASTRUCTURE or run.status == RunStatus.CANCELLED:
            return ExecResult(status="infra_failed", error=msg, result=wire)
        return ExecResult(status="failed", error=msg, result=wire)  # a cell raised: a result about the paper's code
