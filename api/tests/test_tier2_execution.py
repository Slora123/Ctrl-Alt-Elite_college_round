"""Done-when for P6: a paper's clean runs execute on a SEPARATE worker process and the Claim verifier issues a verdict."""
import threading
from datetime import timedelta

import pytest
from test_pipeline_api import NO_GAPS, PRIVATE, PUBLIC, Fetcher, Stack, parsed

from crisisgrid_api.worker.client import ApiClient, ApiError
from crisisgrid_api.worker.executor import ExecResult, SubprocessNotebookExecutor
from crisisgrid_api.worker.loop import Worker
from crisisgrid_core.jobs import Capabilities

GOOD = {"cells": [
    {"cell_type": "markdown", "source": "# train ResNet-20"},
    {"cell_type": "code", "source": "import random\nrandom.seed(SEED)\nacc = 91.0 + random.random() * 0.5\nprint('acc', acc)"},
    {"cell_type": "code", "source": ["report_metric('Top-1', acc)\n"]},
]}
BAD_CELL = {"cells": [{"cell_type": "code", "source": "raise RuntimeError('CUDA out of memory')"}]}


def make_worker(stack, executor=None, caps=None, name="w1"):
    api = ApiClient(stack.client)
    api.register(name, caps or Capabilities(), None)
    return Worker(api, executor or SubprocessNotebookExecutor(), heartbeat_s=1000)


def start(stack, paper_id, notebook=None, seeds=None):
    body = {k: v for k, v in (("notebook", notebook), ("seeds", seeds)) if v is not None}
    r = stack.client.post(f"/papers/{paper_id}/runs", json=body)
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def drain(worker, limit=10):
    n = 0
    while n < limit and worker.run_once():
        n += 1
    return n


@pytest.fixture
def db(migrated_url):
    return migrated_url


def test_clean_runs_on_a_worker_produce_a_verdict(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    run_id = start(s, pid, GOOD)
    assert s.state(run_id)["status"] == "waiting_jobs"
    jobs = s.client.get("/jobs", params={"run_id": run_id}).json()
    assert [j["status"] for j in jobs] == ["queued"] * 3 and [j["spec"]["seed"] for j in jobs] == [0, 1, 2]
    assert all("payload" not in j["spec"] for j in jobs)

    w = make_worker(s)
    assert drain(w) == 3
    st = s.state(run_id)
    assert (st["status"], st["node"]) == ("waiting_gate", "final_gate")

    [c] = s.client.get(f"/papers/{pid}/claims").json()
    v = c["verdict"]
    assert v["status"] == "reproduced" and v["n_seeds"] == 3 and len(v["evidence_run_ids"]) == 3
    assert 91.0 <= v["measured_mean"] <= 91.5 and abs(v["delta"]) < 0.5
    assert not any("few seeds" in cv for cv in v["caveats"])

    md = s.client.get(f"/reports/{pid}").text
    assert "Verdicts (clean runs only)" in md and "**reproduced**" in md and "lower-isolation worker" in md
    assert "No experiment was executed" not in md

    ev = s.events(run_id)
    assert ev.count("job_queued") == 3 and ev.count("job_finished") == 3 and ev.count("verdict_ready") == 1 and "job_log" in ev
    logs = [e.payload["line"] for e in s.container.events.list(run_id) if e.kind == "job_log"]
    assert any(line.startswith("acc 91.") for line in logs)

    s.answer(s.gate(run_id)["id"], "accept")
    assert s.state(run_id)["status"] == "completed"


def test_the_job_payload_the_worker_receives_has_no_claim_target(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    run_id = start(s, s.upload(), GOOD, seeds=[5])
    spec = s.container.jobs.repo.list_jobs(run_id=run_id)[0].spec
    assert set(spec.payload) == {"cells", "requirements"} and "91.25" not in str(spec.payload) and spec.seed == 5


def test_a_failing_notebook_is_not_retried_and_hands_the_decision_to_a_human(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    w = make_worker(s)
    run_id = start(s, pid, BAD_CELL, seeds=[0])
    drain(w)
    jobs = s.container.jobs.repo.list_jobs(run_id=run_id)
    assert [(j.status, j.attempt) for j in jobs] == [("failed", 0)]  # never retried: it is the code's result, not an outage
    g = s.gate(run_id)
    assert g["kind"] == "stuck" and "no clean run produced a result" in g["context"]["reason"] and "CUDA out of memory" in g["context"]["reason"]
    s.answer(g["id"], "skip_stage")
    assert s.state(run_id)["node"] == "final_gate"
    [c] = s.client.get(f"/papers/{pid}/claims").json()
    assert c["verdict"]["status"] == "not_executed"  # no verdict was invented


def test_infrastructure_failure_is_retried_once_and_then_succeeds(db):
    class Flaky:
        calls = 0

        def run(self, payload, *, seed, timeout_s, on_log):
            Flaky.calls += 1
            if Flaky.calls == 1:
                return ExecResult(status="infra_failed", error="worker ran out of memory (killed)")
            return SubprocessNotebookExecutor().run(payload, seed=seed, timeout_s=timeout_s, on_log=on_log)

    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    run_id = start(s, pid, GOOD, seeds=[0])
    drain(make_worker(s, Flaky()), limit=5)
    [j] = s.container.jobs.repo.list_jobs(run_id=run_id)
    assert (j.status, j.attempt) == ("succeeded", 1)
    assert "job_requeued" in s.events(run_id)
    assert s.client.get(f"/papers/{pid}/claims").json()[0]["verdict"]["n_seeds"] == 1


def test_a_crashing_executor_is_reported_as_infrastructure_not_as_a_failed_paper(db):
    class Boom:
        def run(self, *a, **k):
            raise MemoryError("boom")

    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    run_id = start(s, s.upload(), GOOD, seeds=[0])
    drain(make_worker(s, Boom()), limit=4)
    [j] = s.container.jobs.repo.list_jobs(run_id=run_id)
    assert j.status == "infra_failed" and j.attempt == 1  # retried once, then final
    assert s.gate(run_id)["kind"] == "stuck"


def test_with_no_worker_registered_yet_the_jobs_wait_and_run_when_one_connects(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    run_id = start(s, s.upload(), GOOD, seeds=[0])
    assert s.state(run_id)["status"] == "waiting_jobs" and "awaiting_worker" in s.events(run_id)
    assert s.client.get("/gates", params={"run_id": run_id}).json() == []  # waiting is not a failure
    drain(make_worker(s))  # a worker connects later and picks the job up
    assert s.state(run_id)["node"] == "final_gate"


def test_workers_exist_but_none_is_capable_so_a_human_decides(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    make_worker(s, caps=Capabilities(max_runtime_s=10), name="tiny")  # cannot run a 600s-estimated job
    run_id = start(s, s.upload(), GOOD, seeds=[0])
    g = s.gate(run_id)
    assert g["kind"] == "stuck" and "exceeds the worker's limit" in g["context"]["reason"]
    assert s.container.jobs.repo.list_jobs(run_id=run_id) == []  # nothing was queued for a worker that cannot run it
    make_worker(s, name="big")
    assert s.answer(g["id"], "retry_with_hint").status_code == 200
    assert s.state(run_id)["status"] == "waiting_jobs"


def test_worker_that_goes_silent_is_reaped_and_the_job_finishes_elsewhere(db):
    from crisisgrid_core.models import utcnow

    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    dead_api = ApiClient(s.client)
    dead_api.register("dies", Capabilities())
    pid = s.upload()
    run_id = start(s, pid, GOOD, seeds=[0])
    job = dead_api.claim()
    dead_api.start(job["id"])  # claims and starts, then vanishes without a heartbeat or completion
    svc = s.container.jobs
    svc.now = lambda: utcnow() + timedelta(seconds=120)
    [r] = svc.reap()
    svc.now = utcnow
    assert r.status == "queued" and r.attempt == 1 and "job_reaped" in s.events(run_id)
    drain(make_worker(s, name="alive"))
    assert s.state(run_id)["node"] == "final_gate"
    assert s.client.get(f"/papers/{pid}/claims").json()[0]["verdict"]["status"] == "reproduced"


def test_substitute_data_never_yields_a_reproduction(db):
    s = Stack(db, parsed(PRIVATE), NO_GAPS)
    pid = s.upload()
    run_id = start(s, pid, GOOD, seeds=[0, 1, 2])
    assert s.gate(run_id)["kind"] == "data"
    s.answer(s.gate(run_id)["id"], "substitute")
    assert s.state(run_id)["status"] == "waiting_jobs"
    drain(make_worker(s))
    v = s.client.get(f"/papers/{pid}/claims").json()[0]["verdict"]
    assert v["status"] == "inconclusive" and any("not a reproduction" in c for c in v["caveats"])


def test_two_workers_share_the_queue_without_double_running(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    a, b = make_worker(s, name="a"), make_worker(s, name="b")
    run_id = start(s, s.upload(), GOOD, seeds=[0, 1, 2, 3])
    done: list[int] = []

    def go(w):
        done.append(drain(w))

    threads = [threading.Thread(target=go, args=(w,)) for w in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(done) == 4
    jobs = s.container.jobs.repo.list_jobs(run_id=run_id)
    assert sorted(j.status for j in jobs) == ["succeeded"] * 4 and len({j.worker_id for j in jobs}) >= 1
    assert s.state(run_id)["node"] == "final_gate" and s.events(run_id).count("verdict_ready") == 1  # advanced exactly once


# ---- worker API security / contract -----------------------------------------------------------------------------

def test_worker_endpoints_require_a_valid_token(db):
    s = Stack(db, "{}")
    for path in ("/workers/me/claim", "/workers/me/heartbeat"):
        assert s.client.post(path).status_code == 401
        assert s.client.post(path, headers={"Authorization": "Bearer cgw_nope"}).status_code == 401
    assert s.client.post("/jobs/x/start", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_a_worker_cannot_complete_a_job_it_does_not_own(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    owner, thief = make_worker(s, name="owner"), make_worker(s, name="thief")
    start(s, s.upload(), GOOD, seeds=[0])
    job = owner.api.claim()
    with pytest.raises(ApiError) as e:
        thief.api.complete(job["id"], "succeeded", {"metrics": {"top1": 91.25}})
    assert e.value.status == 409


def test_registration_is_closed_outside_dev_without_a_shared_secret(migrated_url):
    from fastapi.testclient import TestClient

    from crisisgrid_api.config import Settings
    from crisisgrid_api.container import build_container
    from crisisgrid_api.db.base import make_engine
    from crisisgrid_api.main import create_app

    def client(**kw):
        st = Settings(database_url=migrated_url, **kw)
        eng = make_engine(migrated_url)
        return TestClient(create_app(st, eng, build_container(eng, st, fetcher=Fetcher())))

    body = {"name": "w", "capabilities": {}}
    assert client(env="prod").post("/workers/register", json=body).status_code == 403
    c = client(env="prod", worker_registration_token="s3cret")
    assert c.post("/workers/register", json=body).status_code == 403
    assert c.post("/workers/register", json=body, headers={"X-Registration-Token": "wrong"}).status_code == 403
    r = c.post("/workers/register", json=body, headers={"X-Registration-Token": "s3cret"})
    assert r.status_code == 201 and r.json()["token"].startswith("cgw_")
    assert client().post("/workers/register", json=body).status_code == 201  # dev is open


def test_notebook_validation(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    for bad in ({"cells": []}, {"cells": [{"cell_type": "markdown", "source": "x"}]}, {"nope": 1}, {"cells": ["str"]},
                {"cells": [{"cell_type": "weird", "source": "x"}]}):
        r = s.client.post(f"/papers/{pid}/runs", json={"notebook": bad})
        assert r.status_code == 422, bad
    assert s.client.post(f"/papers/{pid}/runs", json={"seeds": []}).status_code == 422
    assert s.client.get(f"/papers/{pid}").json()["runs"] == []  # nothing was started by the bad requests


def test_without_a_notebook_nothing_is_executed_and_we_say_so(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    run_id = s.run(pid)
    assert s.state(run_id)["node"] == "final_gate" and s.container.jobs.repo.list_jobs(run_id=run_id) == []
    assert "No experiment was executed" in s.client.get(f"/reports/{pid}").text
