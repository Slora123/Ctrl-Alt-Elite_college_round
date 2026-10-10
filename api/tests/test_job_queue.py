from datetime import datetime, timedelta, timezone

import pytest

from crisisgrid_api.db.base import make_engine, make_session_factory
from crisisgrid_api.db.event_store import SqlEventStore
from crisisgrid_api.db.job_repo import SqlJobRepo
from crisisgrid_api.jobs.service import JobError, JobService, Unauthorized
from crisisgrid_core.harness import FirewallViolation
from crisisgrid_core.jobs import Capabilities, JobRequirements, JobSpec


class Clock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def stack(migrated_url):
    f = make_session_factory(make_engine(migrated_url))
    clk, finished = Clock(), []
    events = SqlEventStore(f)
    svc = JobService(SqlJobRepo(f), events, heartbeat_timeout=timedelta(seconds=60), on_finished=finished.append, now=clk)
    return svc, clk, finished, events


def spec(**kw):
    kw.setdefault("run_id", "r1")
    kw.setdefault("graph_id", "g")
    return JobSpec(**kw)


def kinds(events, run_id="r1"):
    return [e.kind for e in events.list(run_id)]


def test_register_authenticate_and_token_is_not_stored(stack, migrated_url):
    svc, *_ = stack
    w, token = svc.register_worker("laptop", Capabilities())
    assert token.startswith("cgw_") and svc.authenticate(token).id == w.id
    for bad in (None, "", "cgw_nope", token + "x"):
        with pytest.raises(Unauthorized):
            svc.authenticate(bad)
    import sqlite3
    raw = sqlite3.connect(migrated_url.replace("sqlite:///", "")).execute("select token_hash from workers").fetchone()[0]
    assert token not in raw and len(raw) == 64


def test_happy_path_queued_assigned_running_succeeded(stack):
    svc, clk, finished, events = stack
    w, _ = svc.register_worker("w", Capabilities())
    job = svc.enqueue(spec())
    claimed = svc.claim(w)
    assert claimed.id == job.id and claimed.status == "assigned" and claimed.worker_id == w.id
    assert svc.claim(w) is None  # nothing else queued
    assert svc.start(job.id, w.id).status == "running"
    svc.log(job.id, w.id, ["epoch 1", "hf_" + "a" * 30])
    done = svc.complete(job.id, w.id, "succeeded", {"metrics": {"top1": 91.2}})
    assert done.status == "succeeded" and done.result == {"metrics": {"top1": 91.2}} and [j.id for j in finished] == [job.id]
    assert kinds(events) == ["job_queued", "job_assigned", "job_started", "job_log", "job_log", "job_finished"]
    logs = [e.payload["line"] for e in events.list("r1") if e.kind == "job_log"]
    assert logs[0] == "epoch 1" and "hf_" not in logs[1]  # scrubbed server-side as well


def test_jobs_are_claimed_fifo_and_only_by_one_worker(stack):
    svc, *_ = stack
    a, _ = svc.register_worker("a", Capabilities())
    b, _ = svc.register_worker("b", Capabilities())
    j1, j2 = svc.enqueue(spec()), svc.enqueue(spec())
    assert svc.claim(a).id == j1.id and svc.claim(b).id == j2.id
    assert svc.claim(a) is None and svc.claim(b) is None


def test_try_assign_is_compare_and_set(stack):
    svc, *_ = stack
    a, _ = svc.register_worker("a", Capabilities())
    b, _ = svc.register_worker("b", Capabilities())
    j = svc.enqueue(spec())
    now = svc.now()
    assert svc.repo.try_assign(j.id, a.id, now) is True
    assert svc.repo.try_assign(j.id, b.id, now) is False  # lost the race


def test_capability_matching_skips_unsuitable_workers(stack):
    svc, *_ = stack
    cpu, _ = svc.register_worker("cpu", Capabilities(gpu=False))
    gpu, _ = svc.register_worker("gpu", Capabilities(gpu=True))
    j = svc.enqueue(spec(requirements=JobRequirements(gpu_required=True)))
    assert svc.claim(cpu) is None
    assert svc.claim(gpu).id == j.id
    assert svc.unschedulable_reasons(spec(requirements=JobRequirements(est_runtime_s=10**6))) != []
    assert svc.unschedulable_reasons(spec()) == []


def test_unschedulable_reasons_with_no_workers(stack):
    assert stack[0].unschedulable_reasons(spec()) == ["no worker is registered"]


def test_user_code_failure_and_timeout_are_final_never_retried(stack):
    svc, _, finished, _ = stack
    w, _ = svc.register_worker("w", Capabilities())
    for status in ("failed", "timeout"):
        j = svc.enqueue(spec())
        svc.claim(w)
        svc.start(j.id, w.id)
        assert svc.complete(j.id, w.id, status, error="boom").status == status
    assert [j.status for j in finished] == ["failed", "timeout"] and svc.claim(w) is None


def test_infra_failure_is_requeued_once_then_final(stack):
    svc, _, finished, events = stack
    w, _ = svc.register_worker("w", Capabilities())
    j = svc.enqueue(spec())
    svc.claim(w)
    svc.start(j.id, w.id)
    again = svc.complete(j.id, w.id, "infra_failed", error="OOM killed")
    assert again.status == "queued" and again.attempt == 1 and again.worker_id is None and finished == []
    assert "job_requeued" in kinds(events)
    svc.claim(w)
    svc.start(j.id, w.id)
    final = svc.complete(j.id, w.id, "infra_failed", error="OOM killed")
    assert final.status == "infra_failed" and final.attempt == 1 and [x.status for x in finished] == ["infra_failed"]


def test_max_infra_retries_zero_means_no_retry(stack):
    svc, *_ = stack
    w, _ = svc.register_worker("w", Capabilities())
    j = svc.enqueue(spec(max_infra_retries=0))
    svc.claim(w)
    assert svc.complete(j.id, w.id, "infra_failed").status == "infra_failed"  # may be reported before starting


def test_reaper_requeues_a_job_whose_worker_went_silent_then_fails_it(stack):
    svc, clk, finished, events = stack
    dead, _ = svc.register_worker("dead", Capabilities())
    alive, _ = svc.register_worker("alive", Capabilities())
    j = svc.enqueue(spec())
    svc.claim(dead)
    svc.start(j.id, dead.id)
    clk.advance(seconds=30)
    svc.heartbeat(alive.id)
    assert svc.reap() == []                      # 30s of silence is fine
    clk.advance(seconds=40)
    svc.heartbeat(alive.id)
    [r] = svc.reap()                              # 70s > 60s: worker lost
    assert r.status == "queued" and r.attempt == 1 and "job_reaped" in kinds(events)
    assert svc.claim(alive).id == j.id
    svc.start(j.id, alive.id)
    clk.advance(seconds=120)
    [r] = svc.reap()
    assert r.status == "infra_failed" and "heartbeat" in r.error and [x.status for x in finished] == ["infra_failed"]


def test_worker_cannot_touch_someone_elses_job_or_misuse_states(stack):
    svc, *_ = stack
    a, _ = svc.register_worker("a", Capabilities())
    b, _ = svc.register_worker("b", Capabilities())
    j = svc.enqueue(spec())
    svc.claim(a)
    with pytest.raises(JobError):
        svc.start(j.id, b.id)
    with pytest.raises(JobError):
        svc.complete(j.id, a.id, "succeeded")      # not running yet
    with pytest.raises(JobError):
        svc.complete(j.id, a.id, "cancelled")
    svc.start(j.id, a.id)
    svc.complete(j.id, a.id, "succeeded")
    with pytest.raises(JobError):
        svc.complete(j.id, a.id, "failed")          # already final
    with pytest.raises(KeyError):
        svc.start("nope", a.id)


def test_cancel(stack):
    svc, _, finished, _ = stack
    j = svc.enqueue(spec())
    assert svc.cancel(j.id).status == "cancelled" and finished[-1].status == "cancelled"
    with pytest.raises(JobError):
        svc.cancel(j.id)


def test_firewall_blocks_claim_targets_in_job_payloads(stack):
    svc, *_ = stack
    with pytest.raises(FirewallViolation):
        svc.enqueue(spec(payload={"cells": ["x=1"], "reported_value": 91.2}))
    with pytest.raises(FirewallViolation):
        svc.enqueue(spec(payload={"meta": {"claims": []}}))
    svc.enqueue(spec(payload={"cells": ["x=1"], "metric_names": ["top1"]}))
