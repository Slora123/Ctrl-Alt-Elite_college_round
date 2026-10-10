from __future__ import annotations

import logging
import threading
from typing import Any

from crisisgrid_core.scrub import scrub

from .client import ApiClient, ApiError
from .executor import ExecResult, Executor

log = logging.getLogger("crisisgrid.worker")


class Worker:
    """Claims one job at a time, runs it, streams scrubbed logs and reports how it ended."""

    def __init__(self, api: ApiClient, executor: Executor, *, log_batch: int = 20, heartbeat_s: float = 10.0) -> None:
        self.api, self.executor, self.log_batch, self.heartbeat_s = api, executor, log_batch, heartbeat_s

    def run_once(self) -> bool:
        job = self.api.claim()
        if job is None:
            return False
        job_id, spec = job["id"], job["spec"]
        self.api.start(job_id)
        buf: list[str] = []
        lock = threading.Lock()

        def flush() -> None:
            with lock:
                lines, buf[:] = list(buf), []
            try:
                self.api.logs(job_id, lines)
            except ApiError:
                log.warning("could not send logs", extra={"job_id": job_id})

        def on_log(line: str) -> None:
            with lock:
                buf.append(line)
                full = len(buf) >= self.log_batch
            if full:
                flush()

        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_s):
                try:
                    self.api.heartbeat()
                except Exception:  # noqa: BLE001  (a missed beat is not fatal; the reaper decides)
                    log.warning("heartbeat failed")

        threading.Thread(target=beat, daemon=True).start()
        try:
            res = self.executor.run(spec["payload"], seed=spec["seed"], timeout_s=spec["timeout_s"], on_log=on_log)
        except Exception as e:  # noqa: BLE001  (a crash of our own executor is an infrastructure failure, not the paper's fault)
            log.exception("executor crashed")
            res = ExecResult(status="infra_failed", error=scrub(f"worker error: {type(e).__name__}: {e}"))
        finally:
            stop.set()
        flush()
        self.api.complete(job_id, res.status, _wire(res), res.error)
        return True

    def run_forever(self, poll_s: float = 2.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                if not self.run_once():
                    stop.wait(poll_s)
            except ApiError as e:
                log.warning("api error", extra={"error": str(e)})
                stop.wait(poll_s)


def _wire(res: ExecResult) -> dict[str, Any]:
    keep = ("metrics", "hardware_info", "lockfile_digest", "seed", "duration_s", "seeded", "cells_run", "error")
    return {k: res.result[k] for k in keep if k in res.result}
