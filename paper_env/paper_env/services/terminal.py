"""Interactive shell inside a PaperEnv's workspace (the "Terminal" panel).

This is a REAL shell as the server's OS user: it is not sandboxed and it bypasses the cell-level pre-execute hook for
whatever is typed. Therefore it is disabled by default (``Settings.enable_terminal``), limited to researcher/admin roles,
opening one goes through the hook as an ``open_terminal`` action, output is scrubbed best-effort, and when the terminal
closes the environment is re-frozen so package changes made there are recorded as unmanaged lockfile changes.
POSIX only.
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

from ..config import Settings
from ..security import Scrubber


class TerminalError(RuntimeError):
    pass


class TerminalSession:
    def __init__(self, cwd: Path, env: dict[str, str], shell: str, scrubber: Scrubber):
        self.scrubber = scrubber
        master, slave = pty.openpty()
        self.master = master
        try:
            self.proc = subprocess.Popen(
                [shell, "-i"], stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, close_fds=True,
                start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        finally:
            os.close(slave)
        self.closed = False
        self.last_io = time.monotonic()

    def read(self, timeout: float = 0.2) -> str | None:
        """Scrubbed output, '' if nothing yet, None once the shell has exited."""
        if self.closed:
            return None
        r, _, _ = select.select([self.master], [], [], timeout)
        if not r:
            return None if self.proc.poll() is not None else ""
        try:
            data = os.read(self.master, 4096)
        except OSError:
            return None
        if not data:
            return None
        self.last_io = time.monotonic()
        return self.scrubber.scrub(data.decode("utf-8", "replace"))

    def write(self, text: str) -> None:
        if not self.closed:
            self.last_io = time.monotonic()
            os.write(self.master, text.encode())

    def resize(self, rows: int, cols: int) -> None:
        if not self.closed:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            os.killpg(self.proc.pid, signal.SIGHUP)
            time.sleep(0.1)
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass


class TerminalService:
    MAX_PER_ENV = 2

    def __init__(self, settings: Settings, scrubber: Scrubber):
        self.settings, self.scrubber = settings, scrubber
        self._open: dict[str, list[TerminalSession]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.enable_terminal and os.name == "posix"

    def open(self, paper_env_id: str) -> TerminalSession:
        if not self.enabled:
            raise TerminalError("the terminal is disabled (set PAPER_ENV_ENABLE_TERMINAL=1 on the server to enable it)")
        venv = self.settings.venv_dir(paper_env_id)
        if not (venv / "bin" / "python").exists():
            raise TerminalError("the environment has not been created yet")
        cwd = self.settings.files_dir(paper_env_id)
        cwd.mkdir(parents=True, exist_ok=True)
        home = self.settings.env_dir(paper_env_id) / "home"  # keep dotfiles / history out of the server user's real $HOME
        home.mkdir(parents=True, exist_ok=True)
        env = self.scrubber.subprocess_env()
        env.update({"VIRTUAL_ENV": str(venv), "PATH": f"{venv / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}", "HOME": str(home),
                    "TERM": "xterm-256color", "PS1": r"(paper-env) \w $ ", "PYTHONUNBUFFERED": "1"})
        shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        with self._lock:
            live = [t for t in self._open.get(paper_env_id, []) if not t.closed]
            if len(live) >= self.MAX_PER_ENV:
                raise TerminalError(f"at most {self.MAX_PER_ENV} terminals per environment")
            t = TerminalSession(cwd, env, shell, self.scrubber)
            self._open[paper_env_id] = live + [t]
        return t

    def close_all(self) -> None:
        with self._lock:
            for ts in self._open.values():
                for t in ts:
                    t.close()
            self._open.clear()
