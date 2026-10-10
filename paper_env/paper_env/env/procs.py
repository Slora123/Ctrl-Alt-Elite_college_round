"""Run subprocesses with a hard timeout, full log capture and scrubbing."""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..security import Scrubber


@dataclass
class ProcResult:
    returncode: int | None  # None if it could not be started
    output: str  # scrubbed combined stdout+stderr
    timed_out: bool = False
    start_error: str | None = None
    stdout: str = ""  # scrubbed stdout alone (only when merge_stderr=False)


def run_logged(cmd: Sequence[str], *, timeout: float, log_path: Path, scrubber: Scrubber,
               env: dict[str, str] | None = None, cwd: Path | None = None,
               merge_stderr: bool = True) -> ProcResult:
    """Run ``cmd`` (no shell). Full scrubbed output is written to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"$ {' '.join(cmd)}\n"
    try:
        proc = subprocess.Popen(
            list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE, text=True, errors="replace",
            env=env if env is not None else scrubber.subprocess_env(), cwd=cwd, start_new_session=True,
        )
    except OSError as e:
        msg = scrubber.scrub(f"{header}failed to start: {e}\n")
        log_path.write_text(msg)
        return ProcResult(None, msg, start_error=str(e))
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        out = (out or "") + f"\n[timed out after {timeout}s and was killed]\n"
    text = scrubber.scrub(header + (out or "") + (err or ""))
    log_path.write_text(text)
    return ProcResult(proc.returncode, text, timed_out=timed_out,
                      stdout="" if merge_stderr else scrubber.scrub(out or ""))
