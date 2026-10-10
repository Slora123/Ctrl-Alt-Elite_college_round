"""Job queue service: worker registration/auth, atomic claiming with capability matching, log streaming, completion with
infra-only retry, and the heartbeat reaper (system_design 7.1, 7.2)."""
from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from crisisgrid_core.harness import assert_no_targets
from crisisgrid_core.jobs import Capabilities, JobSpec, can_transition, failure_outcome, is_stale, match
from crisisgrid_core.models import utcnow
from crisisgrid_core.scrub import scrub

from ..db.job_repo import SqlJobRepo
from ..repos.interfaces import EventStore, RunEvent
from .models import Job, Worker

FINAL_STATUSES = ("succeeded", "failed", "timeout", "infra_failed")


class Unauthorized(Exception):
    pass


class JobError(ValueError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class JobService:
    def __init__(self, repo: SqlJobRepo, events: EventStore, *, heartbeat_timeout: timedelta = timedelta(seconds=60),
                 on_finished: Callable[[Job], None] | None = None, now: Callable[[], datetime] = utcnow) -> None:
        self.repo, self.events, self.timeout, self.on_finished, self.now = repo, events, heartbeat_timeout, on_finished, now

    def _emit(self, job: Job, kind: str, **payload: Any) -> None:
        self.events.append(RunEvent(run_id=job.run_id, module="jobs", kind=kind, payload={"job_id": job.id, **payload}))

    # ---- workers -------------------------------------------------------------------------------------------
    def register_worker(self, name: str, caps: Capabilities) -> tuple[Worker, str]:
        """Returns the worker and its bearer token. The token is shown once; only its hash is stored."""
        token = "cgw_" + secrets.token_urlsafe(32)
        w = Worker(name=name, capabilities=caps, created_at=self.now(), last_seen=self.now())
        self.repo.add_worker(w, _hash(token))
        return w, token

    def authenticate(self, token: str | None) -> Worker:
        w = self.repo.get_worker_by_token(_hash(token)) if token else None
        if w is None:
            raise Unauthorized("invalid worker token")
        return w

    def heartbeat(self, worker_id: str) -> None:
        self.repo.touch_worker(worker_id, self.now())

    # ---- jobs ----------------------------------------------------------------------------------------------
    def enqueue(self, spec: JobSpec) -> Job:
        assert_no_targets(spec.payload, agent="job_payload")  # executors never see claim targets
        job = Job(id=spec.id, run_id=spec.run_id, spec=spec, created_at=self.now())
        self.repo.add_job(job)
        self._emit(job, "job_queued", mode=spec.mode, seed=spec.seed)
        return job

    def unschedulable_reasons(self, spec: JobSpec) -> list[str]:
        """Why no registered worker can run this job (empty if at least one can)."""
        workers = self.repo.list_workers()
        if not workers:
            return ["no worker is registered"]
        reasons: list[str] = []
        for w in workers:
            ok, why = match(spec.requirements, w.capabilities)
            if ok:
                return []
            reasons.append(f"{w.name}: {'; '.join(why)}")
        return reasons

    def claim(self, worker: Worker) -> Job | None:
        self.heartbeat(worker.id)
        for job in self.repo.list_jobs(statuses=["queued"]):
            if not match(job.spec.requirements, worker.capabilities)[0]:
                continue
            if self.repo.try_assign(job.id, worker.id, self.now()):
                job = self.repo.get_job(job.id) or job
                self._emit(job, "job_assigned", worker_id=worker.id, attempt=job.attempt)
                return job
        return None

    def _owned(self, job_id: str, worker_id: str) -> Job:
        job = self.repo.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.worker_id != worker_id:
            raise JobError("job is not assigned to this worker")
        return job

    def start(self, job_id: str, worker_id: str) -> Job:
        job = self._owned(job_id, worker_id)
        if not can_transition(job.status, "running"):
            raise JobError(f"cannot start a job that is {job.status}")
        job.status, job.started_at = "running", self.now()
        self.repo.save_job(job)
        self._emit(job, "job_started", worker_id=worker_id)
        return job

    def log(self, job_id: str, worker_id: str, lines: list[str], *, secrets_to_hide: list[str] | None = None) -> None:
        job = self._owned(job_id, worker_id)
        self.heartbeat(worker_id)
        for ln in lines:  # the worker scrubs too; scrubbing again server-side means a buggy worker cannot leak
            self._emit(job, "job_log", line=scrub(ln, secrets_to_hide or [])[:4000])

    def complete(self, job_id: str, worker_id: str, status: str, result: dict[str, Any] | None = None, error: str | None = None,
                 *, notify: bool = True) -> Job:
        if status not in FINAL_STATUSES:
            raise JobError(f"status must be one of {FINAL_STATUSES}")
        job = self._owned(job_id, worker_id)
        if status == "infra_failed" and job.status == "assigned":
            pass  # a worker may report an infrastructure failure before it started
        elif not can_transition(job.status, status):
            raise JobError(f"cannot move a {job.status} job to {status}")
        return self._finish(job, status, result, error, notify=notify)

    def _finish(self, job: Job, status: str, result: dict[str, Any] | None, error: str | None, *, notify: bool = True) -> Job:
        if failure_outcome(status, job.attempt, job.spec.max_infra_retries) == "requeue":
            self._emit(job, "job_requeued", reason=error or "infrastructure failure", attempt=job.attempt + 1)
            job.status, job.attempt, job.worker_id = "queued", job.attempt + 1, None
            job.assigned_at = job.started_at = None
            job.error = error
            self.repo.save_job(job)
            return job
        job.status, job.result, job.error, job.finished_at = status, result, scrub(error) if error else None, self.now()  # type: ignore[assignment]
        self.repo.save_job(job)
        self._emit(job, "job_finished", status=status, attempt=job.attempt)
        if self.on_finished and notify:
            self.on_finished(job)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.repo.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if not can_transition(job.status, "cancelled"):
            raise JobError(f"cannot cancel a {job.status} job")
        job.status, job.finished_at = "cancelled", self.now()
        self.repo.save_job(job)
        self._emit(job, "job_finished", status="cancelled", attempt=job.attempt)
        if self.on_finished:
            self.on_finished(job)
        return job

    def reap(self) -> list[Job]:
        """Jobs whose worker stopped heartbeating are infrastructure failures: requeue once, then fail for good."""
        reaped: list[Job] = []
        now = self.now()
        for job in self.repo.list_jobs(statuses=["assigned", "running"]):
            w = self.repo.get_worker(job.worker_id) if job.worker_id else None
            if w is None or is_stale(w.last_seen, now, self.timeout):
                self._emit(job, "job_reaped", worker_id=job.worker_id)
                reaped.append(self._finish(job, "infra_failed", None, "worker lost (heartbeat timeout)"))
        return reaped
