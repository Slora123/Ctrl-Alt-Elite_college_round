from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update

from crisisgrid_core.jobs import JobSpec
from crisisgrid_core.models import as_utc

from ..jobs.models import Job, Worker
from .base import SqlRepository
from .tables import JobRow, WorkerRow


def _u(dt: datetime | None) -> datetime | None:
    return None if dt is None else as_utc(dt)


def _worker(r: WorkerRow) -> Worker:
    return Worker(id=r.id, name=r.name, capabilities=r.capabilities, created_at=as_utc(r.created_at), last_seen=as_utc(r.last_seen))  # type: ignore[arg-type]


def _job(r: JobRow) -> Job:
    return Job(id=r.id, run_id=r.run_id, status=r.status, attempt=r.attempt, spec=JobSpec.model_validate(r.spec),  # type: ignore[arg-type]
               worker_id=r.worker_id, result=r.result, error=r.error, created_at=as_utc(r.created_at),
               assigned_at=_u(r.assigned_at), started_at=_u(r.started_at), finished_at=_u(r.finished_at))


class SqlJobRepo(SqlRepository):
    # ---- workers
    def add_worker(self, w: Worker, token_hash: str) -> None:
        with self._scope() as s:
            s.add(WorkerRow(id=w.id, name=w.name, token_hash=token_hash, capabilities=w.capabilities.model_dump(mode="json"),
                            created_at=w.created_at, last_seen=w.last_seen))

    def get_worker(self, worker_id: str) -> Worker | None:
        with self._scope() as s:
            r = s.get(WorkerRow, worker_id)
            return None if r is None else _worker(r)

    def get_worker_by_token(self, token_hash: str) -> Worker | None:
        with self._scope() as s:
            r = s.scalars(select(WorkerRow).where(WorkerRow.token_hash == token_hash)).first()
            return None if r is None else _worker(r)

    def touch_worker(self, worker_id: str, when: datetime) -> None:
        with self._scope() as s:
            s.execute(update(WorkerRow).where(WorkerRow.id == worker_id).values(last_seen=when))

    def list_workers(self) -> list[Worker]:
        with self._scope() as s:
            return [_worker(r) for r in s.scalars(select(WorkerRow).order_by(WorkerRow.id))]

    # ---- jobs
    def add_job(self, j: Job) -> None:
        with self._scope() as s:
            s.add(JobRow(id=j.id, run_id=j.run_id, status=j.status, attempt=j.attempt, spec=j.spec.model_dump(mode="json"),
                         worker_id=j.worker_id, result=j.result, error=j.error, created_at=j.created_at))

    def get_job(self, job_id: str) -> Job | None:
        with self._scope() as s:
            r = s.get(JobRow, job_id)
            return None if r is None else _job(r)

    def list_jobs(self, *, run_id: str | None = None, statuses: list[str] | None = None) -> list[Job]:
        q = select(JobRow)
        if run_id:
            q = q.where(JobRow.run_id == run_id)
        if statuses:
            q = q.where(JobRow.status.in_(statuses))
        with self._scope() as s:
            return [_job(r) for r in s.scalars(q.order_by(JobRow.id))]

    def try_assign(self, job_id: str, worker_id: str, when: datetime) -> bool:
        """Atomic claim: only one worker can move a job queued -> assigned (compare-and-set on status)."""
        with self._scope() as s:
            res = s.execute(update(JobRow).where(JobRow.id == job_id, JobRow.status == "queued")
                            .values(status="assigned", worker_id=worker_id, assigned_at=when))
            return bool(res.rowcount == 1)  # type: ignore[attr-defined]

    def delete_for_runs(self, run_ids: list[str]) -> None:
        if run_ids:
            with self._scope() as s:
                s.execute(delete(JobRow).where(JobRow.run_id.in_(run_ids)))

    def save_job(self, j: Job) -> None:
        with self._scope() as s:
            r = s.get(JobRow, j.id)
            if r is None:
                raise KeyError(j.id)
            r.status, r.attempt, r.worker_id, r.result, r.error = j.status, j.attempt, j.worker_id, j.result, j.error
            r.assigned_at, r.started_at, r.finished_at = j.assigned_at, j.started_at, j.finished_at
