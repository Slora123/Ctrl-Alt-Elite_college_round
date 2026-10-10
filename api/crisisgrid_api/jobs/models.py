from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from crisisgrid_core.ids import new_id
from crisisgrid_core.jobs import Capabilities, JobSpec, JobStatus
from crisisgrid_core.models import utcnow


class Worker(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    capabilities: Capabilities = Field(default_factory=Capabilities)
    created_at: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)


class Job(BaseModel):
    id: str
    run_id: str
    status: JobStatus = "queued"
    attempt: int = 0
    spec: JobSpec
    worker_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    assigned_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
