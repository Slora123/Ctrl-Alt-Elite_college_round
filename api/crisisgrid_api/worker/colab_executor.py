"""Executor that runs clean-run jobs on a Google Colab VM (GPU/TPU/CPU) through the official `colab` CLI
(https://github.com/googlecolab/google-colab-cli, `uv tool install google-colab-cli`).

How a job runs: one Colab session is created lazily and reused for the worker's jobs (allocating a VM takes about a minute).
Before each job the kernel is restarted, so every seed is still a clean run in a fresh process. The notebook is executed by the
same driver the local worker uses (`driver.run_spec`), sent as one script; the driver prints its result between markers, which
is how metrics, errors and the GPU name come back. Output is streamed line by line to the run's event log.

Failure classification matches the other executors: a cell that raises is `failed` (a fact about the code), whereas anything
that goes wrong with Colab itself (login, quota, no VM, lost connection, no result marker) is `infra_failed` and never evidence.

What this is NOT: a sandbox we control. The notebook and its output leave this machine and run on Google's VM (the worker
advertises `isolation="remote_vm"` so the report says so). Needs Linux or macOS and a Google login done once by a human.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from crisisgrid_core.scrub import scrub

from .executor import DRIVER, ExecResult

MARK = "@@CG_RESULT@@"
GPUS = ("T4", "L4", "G4", "H100", "A100")
# already on Colab images: installing them unpinned only wastes minutes (and can replace a CUDA build)
PREINSTALLED = {"numpy", "scipy", "pandas", "scikit-learn", "matplotlib", "seaborn", "tqdm", "pillow", "torch", "torchvision",
                "torchaudio", "tensorflow", "keras", "jax", "jaxlib", "flax", "optax", "transformers", "datasets", "tokenizers",
                "statsmodels", "networkx", "sympy", "opencv-python-headless", "einops", "scikit-image"}
AUTH_HINT = re.compile(r"401|403|unauthori[sz]ed|unauthenticated|authori[sz]e colab-cli|credentials|invalid_grant|consent", re.I)
QUOTA_HINT = re.compile(r"\b400\b|quota|entitle|not available|no (?:gpu|accelerator)|unavailable", re.I)


class ColabError(RuntimeError):
    """Colab itself failed (not the experiment)."""


def find_colab() -> str | None:
    return shutil.which("colab") or next((str(p) for p in (Path.home() / ".local/bin/colab",) if p.exists()), None)


def login_help(extra: str = "") -> str:
    return (extra + " " if extra else "") + (
        "Log in to Colab once from a terminal: `colab --auth=oauth2 sessions` (open the printed URL, paste the code back), "
        "or `gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/cloud-platform,"
        "https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory` and use --colab-auth adc.")


def driver_script(spec: dict[str, Any]) -> str:
    """The local driver (minus its __main__ block) plus a call that prints the result between markers."""
    src = DRIVER.read_text(encoding="utf-8")
    src = src.split('if __name__ == "__main__":')[0]
    return (src + "\n\nimport json as _j\n"
            f"_spec = _j.loads({json.dumps(json.dumps(spec))})\n"
            f"print({MARK!r} + _j.dumps(run_spec(_spec)), flush=True)\n")


class ColabExecutor:
    def __init__(self, workdir: str | Path, *, gpu: str | None = "T4", tpu: str | None = None, auth: str = "oauth2",
                 high_mem: bool = False, session: str | None = None, idle_stop_s: int = 600, binary: str | None = None,
                 secrets: list[str] | None = None) -> None:
        self.bin = binary or find_colab()
        if not self.bin:
            raise ColabError("the `colab` CLI is not installed: run `uv tool install google-colab-cli` (Linux/macOS only)")
        gpu = (gpu or "").upper() or None
        if gpu and gpu not in GPUS:
            raise ColabError(f"unsupported GPU {gpu!r}: choose one of {', '.join(GPUS)} (an unknown value silently becomes A100)")
        self.gpu, self.tpu, self.high_mem, self.auth = gpu, tpu, high_mem, auth
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state = self.workdir / "colab-sessions.json"  # isolated from the user's own `colab` sessions
        self.session = session or f"cg-{os.getpid()}"
        self.idle_stop_s, self.secrets = idle_stop_s, secrets or []
        self._lock = threading.Lock()
        self._alive = False
        self._installed: set[str] = set()
        self._last_used = time.monotonic()
        self._idle: threading.Timer | None = None
        atexit.register(self.close)

    # ------------------------------------------------------------------ cli plumbing
    def _cmd(self, *args: str) -> list[str]:
        return [self.bin, f"--auth={self.auth}", "--config", str(self.state), *args]  # type: ignore[list-item]

    def _run(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(self._cmd(*args), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as e:
            raise ColabError(f"`colab {args[0]}` did not finish in {timeout:.0f}s (waiting for a login prompt or a VM?)") from e

    def check_login(self) -> None:
        """Fail fast and loudly (before any job is claimed) if the CLI cannot talk to Colab non-interactively."""
        r = self._run(["sessions"], timeout=60)
        out = (r.stdout + r.stderr)
        if r.returncode != 0 or "authorize colab-cli" in out:
            raise ColabError(login_help("The Colab CLI is not logged in."))

    def _ensure_session(self, on_log: Callable[[str], None]) -> None:
        if self._alive:
            st = self._run(["status", "-s", self.session], timeout=60)
            if st.returncode == 0:
                return
            self._alive = False
            self._installed.clear()
        accel = f"GPU {self.gpu}" if self.gpu else f"TPU {self.tpu}" if self.tpu else "CPU"
        on_log(f"[colab] allocating a {accel} runtime (this takes a minute)…")
        args = ["new", "-s", self.session]
        if self.gpu:
            args += ["--gpu", self.gpu]
        elif self.tpu:
            args += ["--tpu", self.tpu]
        if self.high_mem:
            args.append("--high-mem")
        r = self._run(args, timeout=600)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            if AUTH_HINT.search(out):
                raise ColabError(login_help("Colab rejected the credentials."))
            if QUOTA_HINT.search(out) and self.gpu:
                raise ColabError(f"Colab refused a {self.gpu} runtime (no quota or entitlement on this account). "
                                 f"Try --colab-gpu T4, or none for CPU. CLI said: {out[-300:]}")
            raise ColabError(f"could not create a Colab session: {out[-400:]}")
        self._alive = True
        on_log(f"[colab] runtime ready (session {self.session})")

    def _install(self, requirements: list[str], on_log: Callable[[str], None]) -> None:
        todo = [r for r in requirements
                if r not in self._installed and not (re.fullmatch(r"[A-Za-z0-9._-]+", r) and r.lower() in PREINSTALLED)]
        if not todo:
            return
        on_log(f"[colab] installing {', '.join(todo)}")
        r = self._run(["install", "-s", self.session, *todo], timeout=900)
        if r.returncode != 0:
            raise ColabError(f"package install failed on the VM: {(r.stdout + r.stderr).strip()[-400:]}")
        self._installed.update(todo)

    def _schedule_idle_stop(self) -> None:
        if self._idle:
            self._idle.cancel()
        if self.idle_stop_s > 0:
            self._idle = threading.Timer(self.idle_stop_s, self._stop_if_idle)
            self._idle.daemon = True
            self._idle.start()

    def _stop_if_idle(self) -> None:
        if time.monotonic() - self._last_used >= self.idle_stop_s - 1 and self._lock.acquire(blocking=False):
            try:
                self.close()
            finally:
                self._lock.release()

    def close(self) -> None:
        """Release the VM (idle VMs burn compute units)."""
        if self._alive:
            try:
                self._run(["stop", "-s", self.session], timeout=120)
            except Exception:  # noqa: BLE001
                pass
            self._alive = False
            self._installed.clear()

    # ------------------------------------------------------------------ run
    def run(self, payload: dict[str, Any], *, seed: int, timeout_s: int, on_log: Callable[[str], None]) -> ExecResult:
        cells = payload.get("cells")
        if not isinstance(cells, list) or not cells:
            return ExecResult(status="infra_failed", error="job payload has no cells")
        with self._lock:
            if self._idle:
                self._idle.cancel()
            try:
                return self._run_job(cells, list(payload.get("requirements") or []), seed, timeout_s, on_log)
            except ColabError as e:
                return ExecResult(status="infra_failed", error=scrub(str(e), self.secrets))
            finally:
                self._last_used = time.monotonic()
                self._schedule_idle_stop()

    def _run_job(self, cells: list[Any], reqs: list[str], seed: int, timeout_s: int, on_log: Callable[[str], None]) -> ExecResult:
        self._ensure_session(on_log)
        self._install(reqs, on_log)
        rk = self._run(["restart-kernel", "-s", self.session], timeout=120)  # a clean run is a fresh process
        if rk.returncode != 0:
            raise ColabError(f"could not restart the Colab kernel: {(rk.stdout + rk.stderr).strip()[-300:]}")
        script = self.workdir / f"job-{seed}-{os.getpid()}.py"
        script.write_text(driver_script({"cells": cells, "seed": seed}), encoding="utf-8")
        result: dict[str, Any] | None = None
        tail: list[str] = []
        try:
            proc = subprocess.Popen(self._cmd("exec", "-s", self.session, "-f", str(script), "--timeout", str(float(timeout_s))),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace")
        except OSError as e:
            raise ColabError(f"could not start the colab CLI: {e}") from e
        killer = threading.Timer(timeout_s + 90, proc.kill)  # the CLI's own --timeout plus slack for connect/teardown
        killer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith(MARK):
                    try:
                        result = json.loads(line[len(MARK):])
                    except ValueError:
                        pass
                    continue
                tail.append(line)
                del tail[:-40]
                on_log(scrub(line, self.secrets))
            proc.wait()
        finally:
            killer.cancel()
            script.unlink(missing_ok=True)
        if result is None:
            joined = "\n".join(tail)
            if AUTH_HINT.search(joined):
                raise ColabError(login_help("Colab rejected the credentials."))
            if re.search(r"timed? ?out|timeout", joined, re.I):
                return ExecResult(status="timeout", error=f"exceeded the {timeout_s}s time limit on the Colab VM")
            self._alive = False  # the session may be gone: next job re-checks and re-creates it
            raise ColabError(f"no result came back from Colab (exit code {proc.returncode}): {joined[-300:]}")
        err = result.get("error")
        if isinstance(err, dict):
            for k in ("message", "traceback"):
                if isinstance(err.get(k), str):
                    err[k] = scrub(err[k], self.secrets)
        if result.get("status") == "ok":
            return ExecResult(status="succeeded", result=result)
        msg = scrub(f"{err.get('type')}: {err.get('message')}" if err else "cell failed", self.secrets)
        return ExecResult(status="failed", result=result, error=msg)
