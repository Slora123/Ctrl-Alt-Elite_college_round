"""paper_env mounted inside the CrisisGrid API (one process, one login), and the worker executor that runs jobs in its kernels."""
import json

import pytest

pytest.importorskip("paper_env", reason="paper_env is not installed in this environment")
pytest.importorskip("fcntl", reason="paper_env needs POSIX")

from fastapi.testclient import TestClient

from crisisgrid_api.config import Settings
from crisisgrid_api.main import create_app

SECRET = "unit-test-secret-0123456789abcdef"
PW = "correct-horse-battery"


def H(tok):
    return {"authorization": f"Bearer {tok}"}


@pytest.fixture
def client(migrated_url, tmp_path):
    s = Settings(database_url=migrated_url, auth_enabled=True, auth_secret=SECRET, reaper_interval_s=0, paper_env_enabled=True,
                 paper_env_dir=str(tmp_path / "pe"))
    with TestClient(create_app(s)) as c:
        yield c


def reg(c, email):
    return c.post("/auth/register", json={"email": email, "name": email.split("@")[0], "password": PW}).json()


def test_mounted_environment_is_reported_by_health(client):
    h = client.get("/health").json()
    assert h["paper_env"] is True and h["auth"] is True
    assert client.get("/paper-env/health").json()["status"] == "ok"  # liveness of the mounted app is public


def test_platform_login_covers_the_mounted_api(client):
    assert client.get("/paper-env/me").status_code == 401
    assert client.get("/paper-env/paper-envs").status_code == 401
    assert client.get("/paper-env/me", headers=H("garbage")).status_code == 401
    ada = reg(client, "ada@example.com")
    me = client.get("/paper-env/me", headers=H(ada["token"])).json()
    assert me == {"id": ada["user"]["id"], "name": "ada", "role": "admin"}
    # an API token issued by the platform works there too
    api_tok = client.post("/auth/tokens", headers=H(ada["token"]), json={"name": "t"}).json()
    assert client.get("/paper-env/me", headers=H(api_tok["token"])).status_code == 200
    client.delete(f"/auth/tokens/{api_tok['id']}", headers=H(ada["token"]))
    assert client.get("/paper-env/me", headers=H(api_tok["token"])).status_code == 401


def test_revocation_and_roles_apply_inside_the_mounted_api(client):
    ada, bob = reg(client, "ada@example.com"), reg(client, "bob@example.com")
    assert client.get("/paper-env/me", headers=H(bob["token"])).json()["role"] == "researcher"
    client.patch(f"/auth/users/{bob['user']['id']}", headers=H(ada["token"]), json={"role": "viewer"})
    assert client.get("/paper-env/me", headers=H(bob["token"])).status_code == 401  # role changed: old token revoked
    vt = client.post("/auth/login", json={"email": "bob@example.com", "password": PW}).json()["token"]
    assert client.get("/paper-env/paper-envs", headers=H(vt)).status_code == 200
    assert client.post("/paper-env/paper-envs", headers=H(vt), json={"alias": "x", "paper_hash": "h"}).status_code == 403
    client.patch(f"/auth/users/{bob['user']['id']}", headers=H(ada["token"]), json={"disabled": True})
    assert client.get("/paper-env/me", headers=H(vt)).status_code == 401


def test_environments_are_owned_by_the_platform_user(client):
    ada, bob = reg(client, "ada@example.com"), reg(client, "bob@example.com")
    r = client.post("/paper-env/paper-envs", headers=H(bob["token"]), json={"alias": "bobs", "paper_hash": "h"})
    eid = r.json()["paper_env"]["id"]
    assert r.json()["paper_env"]["owner_id"] == bob["user"]["id"]
    carol = reg(client, "carol@example.com")
    assert client.get("/paper-env/paper-envs", headers=H(carol["token"])).json() == []
    assert client.get(f"/paper-env/paper-envs/{eid}", headers=H(carol["token"])).status_code == 403
    assert client.get(f"/paper-env/paper-envs/{eid}", headers=H(ada["token"])).status_code == 200  # admin
    assert client.get("/paper-env/paper-envs", headers=H(bob["token"])).json()[0]["alias"] == "bobs"


def test_notebook_ui_and_websocket_are_served_under_the_mount(client):
    ada = reg(client, "ada@example.com")
    ui = client.get("/paper-env/ui/")
    if ui.status_code == 200:  # only when the Vite build exists
        assert "<script" in ui.text and 'src="./assets/' in ui.text  # relative assets so the mount prefix works
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/paper-env/events"):
            pass
    with client.websocket_connect(f"/paper-env/events?token={ada['token']}&after_seq=0") as ws:
        client.post("/paper-env/paper-envs", headers=H(ada["token"]), json={"alias": "ws", "paper_hash": "h"})
        client.post(f"/paper-env/paper-envs/{client.get('/paper-env/paper-envs', headers=H(ada['token'])).json()[0]['id']}/cells",
                    headers=H(ada["token"]), json={"source": "x = 1"})
        assert ws.receive_json()["type"] == "cell_edited"


def test_disabled_mount_leaves_the_rest_working(migrated_url):
    s = Settings(database_url=migrated_url, auth_enabled=False, paper_env_enabled=False, reaper_interval_s=0)
    with TestClient(create_app(s)) as c:
        assert c.get("/health").json()["paper_env"] is False
        assert c.get("/paper-env/health").status_code == 404
        assert c.get("/papers").status_code == 200


# ------------------------------------------------------------------ worker executor
@pytest.fixture(scope="module")
def executor(tmp_path_factory):
    from crisisgrid_api.worker.paper_env_executor import PaperEnvExecutor
    ex = PaperEnvExecutor(tmp_path_factory.mktemp("exec"))
    yield ex
    ex.core.shutdown()


def run(ex, cells, seed=7, t=90, **payload):
    logs: list[str] = []
    res = ex.run({"cells": [{"kind": "code", "source": s} for s in cells], **payload}, seed=seed, timeout_s=t, on_log=logs.append)
    return res, logs


def test_executor_success_returns_the_worker_contract(executor):
    res, logs = run(executor, ["import random\nrandom.seed(SEED)\nreport_metric('acc', 0.9)\nprint('seed', SEED)", "report_metric('loss', 0.1)"])
    assert res.status == "succeeded" and res.error is None
    r = res.result
    assert r["metrics"] == {"acc": 0.9, "loss": 0.1} and r["cells_run"] == 2 and r["seed"] == 7 and r["status"] == "ok"
    assert r["lockfile_digest"] and len(r["lockfile_digest"]) == 64 and r["executor"] == "paper_env"
    assert r["seeded"]["python"] is True and r["hardware_info"]["python"].startswith("3.")
    assert "seed 7" in logs
    json.dumps(r)  # must be JSON-serialisable for the wire


def test_executor_is_deterministic_for_a_fixed_seed(executor):
    cells = ["import random\nrandom.seed(SEED)\nreport_metric('r', random.random())"]
    a, _ = run(executor, cells, seed=11)
    b, _ = run(executor, cells, seed=11)
    c, _ = run(executor, cells, seed=12)
    assert a.result["metrics"] == b.result["metrics"] != c.result["metrics"]
    assert a.result["lockfile_digest"] == b.result["lockfile_digest"]


def test_executor_distinguishes_experiment_failure_from_infrastructure(executor):
    exc, _ = run(executor, ["report_metric('a', 1)", "1/0", "report_metric('b', 2)"])
    assert exc.status == "failed" and "ZeroDivisionError" in exc.error
    assert exc.result["metrics"] == {"a": 1.0} and exc.result["cells_run"] == 1
    assert exc.result["error"]["type"] == "ZeroDivisionError" and exc.result["error"]["cell_index"] == 1
    dead, _ = run(executor, ["import os\nos._exit(3)"])
    assert dead.status == "infra_failed" and "kernel_died" in dead.error
    killed, _ = run(executor, ["import os, signal\nos.kill(os.getpid(), signal.SIGKILL)"])
    assert killed.status == "infra_failed" and "oom_or_killed" in killed.error
    slow, _ = run(executor, ["import time\ntime.sleep(60)"], t=3)
    assert slow.status == "timeout"
    # and the executor is still usable afterwards
    assert run(executor, ["report_metric('ok', 1)"])[0].status == "succeeded"


def test_executor_refuses_magics_and_installs_like_the_subprocess_driver(executor):
    for src in ("!pip install requests", "%pip install x", "!ls", "import subprocess\n!uv pip install x"):
        res, _ = run(executor, [src])
        assert res.status == "failed" and res.result["error"]["type"] == "UnsupportedCell", src


def test_executor_scrubs_secrets_from_logs_and_errors(tmp_path):
    from crisisgrid_api.worker.paper_env_executor import PaperEnvExecutor
    ex = PaperEnvExecutor(tmp_path / "e", secrets=["worker-secret-value-1"])
    try:
        res, logs = run(ex, ["print('token is worker-secret-value-1')\nraise RuntimeError('boom worker-secret-value-1')"])
        assert res.status == "failed"
        assert "worker-secret-value-1" not in " ".join(logs) + (res.error or "") + json.dumps(res.result)
    finally:
        ex.core.shutdown()


def test_executor_reports_a_failed_environment_build_as_infrastructure(executor):
    res, _ = run(executor, ["x = 1"], requirements=["definitely-not-a-real-package-zzz==9.9"], t=120)
    assert res.status == "infra_failed" and "environment" in res.error


def test_executor_rejects_empty_payloads(executor):
    assert executor.run({"cells": []}, seed=0, timeout_s=5, on_log=lambda s: None).status == "infra_failed"
    assert executor.run({}, seed=0, timeout_s=5, on_log=lambda s: None).status == "infra_failed"


def test_each_job_gets_a_fresh_notebook_and_kernel(executor):
    run(executor, ["leaked = 'from job one'\nreport_metric('x', 1)"])
    res, _ = run(executor, ["report_metric('seen', 1.0 if 'leaked' in globals() else 0.0)"])
    assert res.result["metrics"] == {"seen": 0.0}  # no state crosses jobs
