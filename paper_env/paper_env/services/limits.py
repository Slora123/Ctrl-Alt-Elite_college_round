"""Idle timeout, maximum session length and cost cap for kernel sessions (auto-disconnect).

A background thread sweeps live sessions. Expiry shuts the kernel down, marks every executed cell stale (the in-memory
state is gone), and emits ``session_expired`` so the UI and any harness can react. Limits are per PaperEnv
(``PaperEnv.limits``); the cost cap uses the runtime profile's ``cost_per_hr`` (0 for local runtimes).
"""
from __future__ import annotations

import logging
import threading

from ..kernel.runner import KernelRunner
from ..repos import NotFound, UnitOfWorkFactory

log = logging.getLogger(__name__)


class SessionReaper:
    def __init__(self, uow_factory: UnitOfWorkFactory, runner: KernelRunner, interval_s: float = 15.0):
        self._uow, self.runner, self.interval_s = uow_factory, runner, interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval_s > 0 and self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="session-reaper")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.sweep()
            except Exception:  # a failed sweep must never kill the thread; the next one retries
                log.exception("session sweep failed")

    def sweep(self) -> list[tuple[str, str]]:
        """Expire sessions over a limit. Returns [(paper_env_id, reason)]. Public so it can be called directly."""
        expired = []
        for env_id in list(self.runner.sessions):
            info = self.runner.session_info(env_id)
            if info is None or not info["alive"]:
                continue
            try:
                with self._uow() as u:
                    limits = u.paper_envs.get(env_id).limits
            except NotFound:
                self.runner.shutdown(env_id)
                continue
            reason = msg = None
            if limits.max_session_s is not None and info["age_s"] >= limits.max_session_s:
                reason, msg = "max_session", f"the session reached its {limits.max_session_s}s maximum length"
            elif limits.max_cost is not None and info["cost_so_far"] >= limits.max_cost > 0:
                reason, msg = "cost_cap", f"the session reached its cost cap ({limits.max_cost})"
            elif limits.idle_timeout_s is not None and not info["busy"] and info["idle_s"] >= limits.idle_timeout_s:
                reason, msg = "idle", f"the session was idle for {limits.idle_timeout_s}s and was disconnected"
            if reason and self.runner.expire(env_id, reason, msg):
                expired.append((env_id, reason))
        return expired
