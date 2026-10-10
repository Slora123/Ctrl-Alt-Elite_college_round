"""Worker-facing and job endpoints. Workers connect OUT to the API (principle 6): they register, then poll for work with a
bearer token; the backend never connects inward to a user machine."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from crisisgrid_core.jobs import Capabilities

from .auth.models import User
from .config import Settings
from .container import AppContainer
from .jobs.models import Worker
from .jobs.service import JobError, JobService, Unauthorized


class RegisterBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    capabilities: Capabilities = Field(default_factory=Capabilities)


class LogBody(BaseModel):
    lines: list[str] = Field(default_factory=list, max_length=500)


class CompleteBody(BaseModel):
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


def build_worker_router(c: AppContainer, settings: Settings) -> APIRouter:
    r = APIRouter()
    svc: JobService = c.jobs

    def worker(authorization: str | None = Header(default=None)) -> Worker:
        token = authorization.split(" ", 1)[1] if authorization and authorization.lower().startswith("bearer ") else None
        try:
            return svc.authenticate(token)
        except Unauthorized as e:
            raise HTTPException(401, str(e)) from e

    @r.post("/workers/register", status_code=201)
    def register(body: RegisterBody, x_registration_token: str | None = Header(default=None)) -> dict[str, Any]:
        required = settings.worker_registration_token
        if required and x_registration_token != required:
            raise HTTPException(403, "invalid registration token")
        if not required and settings.env != "dev":
            raise HTTPException(403, "worker registration is closed: set CG_WORKER_REGISTRATION_TOKEN")
        w, token = svc.register_worker(body.name, body.capabilities)
        return {"worker_id": w.id, "token": token}  # shown once

    @r.get("/workers")
    def list_workers(_: User = Depends(c.auth_deps.writer)) -> list[dict[str, Any]]:
        return [w.model_dump(mode="json") for w in svc.repo.list_workers()]

    @r.post("/workers/me/heartbeat")
    def heartbeat(w: Worker = Depends(worker)) -> dict[str, str]:
        svc.heartbeat(w.id)
        return {"status": "ok"}

    @r.post("/workers/me/claim", response_model=None)
    def claim(w: Worker = Depends(worker)) -> Any:
        job = svc.claim(w)
        if job is None:
            return {"job": None}
        return {"job": {"id": job.id, "run_id": job.run_id, "attempt": job.attempt, "spec": job.spec.model_dump(mode="json")}}

    def _guard(fn):  # type: ignore[no-untyped-def]
        try:
            return fn()
        except KeyError as e:
            raise HTTPException(404, "job not found") from e
        except JobError as e:
            raise HTTPException(409, str(e)) from e

    @r.post("/jobs/{job_id}/start")
    def start(job_id: str, w: Worker = Depends(worker)) -> dict[str, str]:
        _guard(lambda: svc.start(job_id, w.id))
        return {"status": "running"}

    @r.post("/jobs/{job_id}/logs")
    def logs(job_id: str, body: LogBody, w: Worker = Depends(worker)) -> dict[str, int]:
        _guard(lambda: svc.log(job_id, w.id, body.lines))
        return {"accepted": len(body.lines)}

    @r.post("/jobs/{job_id}/complete")
    def complete(job_id: str, body: CompleteBody, bg: BackgroundTasks, w: Worker = Depends(worker)) -> dict[str, Any]:
        job = _guard(lambda: svc.complete(job_id, w.id, body.status, body.result, body.error, notify=False))
        if job.status != "queued" and c.orchestrator is not None:  # queued = requeued after an infra failure, nothing to tell the run
            bg.add_task(c.orchestrator.on_job_finished, job.run_id)
        return {"status": job.status, "requeued": job.status == "queued"}

    @r.get("/jobs")
    def list_jobs(run_id: str | None = None, user: User = Depends(c.auth_deps.current_user)) -> list[dict[str, Any]]:
        def ok(j: Any) -> bool:
            s = c.runs.get(j.run_id)
            p = c.papers.get(s.paper_id) if s else None
            return user.is_admin or (p is not None and p.owner_id in (None, user.id))
        return [j.model_dump(mode="json", exclude={"spec": {"payload"}}) for j in svc.repo.list_jobs(run_id=run_id) if ok(j)]

    @r.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, bg: BackgroundTasks, user: User = Depends(c.auth_deps.writer)) -> dict[str, str]:
        existing = svc.repo.get_job(job_id) if hasattr(svc.repo, "get_job") else None
        if existing is not None and not user.is_admin:
            st = c.runs.get(existing.run_id)
            p = c.papers.get(st.paper_id) if st else None
            if p is None or p.owner_id not in (None, user.id):
                raise HTTPException(404, "job not found")
        job = _guard(lambda: svc.cancel(job_id))
        return {"status": job.status}

    return r
