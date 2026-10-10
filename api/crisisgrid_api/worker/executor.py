"""Executors run one job and report how it ended. The default is a restricted subprocess ("lower isolation": no Docker),
which the worker advertises honestly in its capabilities so jobs that require strong isolation are never sent to it.

Classification matters: a cell that raises is `failed` (a result about the paper's code), whereas a process that dies,
is killed or produces no result is `infra_failed` (retryable, and never evidence).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from crisisgrid_core.scrub import scrub

DRIVER = Path(__file__).with_name("driver.py")


class ExecResult(BaseModel):
    status: str  # succeeded | failed | timeout | infra_failed
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Executor(Protocol):
    def run(self, payload: dict[str, Any], *, seed: int, timeout_s: int, on_log: Callable[[str], None]) -> ExecResult: ...


def _scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Nothing from the worker's own environment (API tokens, cloud creds) reaches user code. Windows needs SYSTEMROOT."""
    keep = ["PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "VIRTUAL_ENV"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra or {})
    return env


class SubprocessNotebookExecutor:
    def __init__(self, python: str | None = None, secrets: list[str] | None = None) -> None:
        self.python = python or sys.executable
        self.secrets = secrets or []

    def run(self, payload: dict[str, Any], *, seed: int, timeout_s: int, on_log: Callable[[str], None]) -> ExecResult:
        cells = payload.get("cells")
        if not isinstance(cells, list) or not cells:
            return ExecResult(status="infra_failed", error="job payload has no cells")
        with tempfile.TemporaryDirectory(prefix="cg-job-") as tmp:
            tmpd = Path(tmp)
            spec, out = tmpd / "spec.json", tmpd / "result.json"
            spec.write_text(json.dumps({"cells": cells, "seed": seed}), encoding="utf-8")
            try:
                proc = subprocess.Popen([self.python, "-I", str(DRIVER), str(spec), str(out)], cwd=tmp, env=_scrubbed_env(),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            except OSError as e:
                return ExecResult(status="infra_failed", error=f"could not start the interpreter: {e}")

            def pump() -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    on_log(scrub(line.rstrip("\n"), self.secrets))

            t = threading.Thread(target=pump, daemon=True)
            t.start()
            deadline = time.monotonic() + timeout_s
            timed_out = False
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    timed_out = True
                    proc.kill()
                    break
                time.sleep(0.05)
            proc.wait()
            t.join(timeout=5)

            if timed_out:
                return ExecResult(status="timeout", error=f"exceeded the {timeout_s}s time limit and was killed")
            if not out.exists():  # killed by the OS / OOM / crashed in native code before writing anything
                return ExecResult(status="infra_failed", error=f"process exited with code {proc.returncode} without a result")
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            except ValueError:
                return ExecResult(status="infra_failed", error="result file was unreadable")
            err = data.get("error")
            if isinstance(err, dict):  # user code may print or raise with a secret in it; scrub before it leaves the worker
                for k in ("message", "traceback"):
                    if isinstance(err.get(k), str):
                        err[k] = scrub(err[k], self.secrets)
            if data.get("status") == "ok":
                return ExecResult(status="succeeded", result=data)
            msg = scrub(f"{err.get('type')}: {err.get('message')}" if err else "cell failed", self.secrets)
            return ExecResult(status="failed", result=data, error=msg)
