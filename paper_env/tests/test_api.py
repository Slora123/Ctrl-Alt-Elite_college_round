import shutil
import time

import pytest
from fastapi.testclient import TestClient

from paper_env.api import create_api
from paper_env.config import Settings
from paper_env.container import build_app
from paper_env.hooks import Decision
from paper_env.repos.memory import memory_uow_factory


class Ask:
    def check(self, a):
        return Decision.needs_approval("api test")


def make_client(tmp_path, template, hook=None):
    settings = Settings(data_dir=tmp_path / "data", min_free_disk_bytes=0)
    core = build_app(settings, uow_factory=memory_uow_factory(), hook=hook, network_check=lambda m: True)
    client = TestClient(create_api(core))
    client.__enter__()
    return client, core


@pytest.fixture
def api(tmp_path, kernel_template):
    c, core = make_client(tmp_path, kernel_template)
    yield c, core, kernel_template
    c.__exit__(None, None, None)


def ready_env(c, core, template, alias="api-env"):
    r = c.post("/paper-envs", json={"alias": alias, "paper_hash": "h", "seed": 3})
    assert r.status_code == 201
    pid = r.json()["paper_env"]["id"]
    shutil.copytree(template["venv"], core.settings.venv_dir(pid), symlinks=True)
    with core.uow() as u:
        env = u.paper_envs.get(pid)
        spec = u.paper_envs.get_spec(env.env_spec_id)
        spec.lockfile_hash = template["lockfile_hash"]
        u.paper_envs.update_spec(spec)
    return pid


def test_health_and_error_mapping(api):
    c, core, _ = api
    assert c.get("/health").json()["backend"]["backend"] == "local"
    assert c.get("/paper-envs/nope").status_code == 404
    assert c.get("/cells/nope").status_code == 404
    assert c.post("/paper-envs", json={"alias": "a", "paper_hash": "h"}).status_code == 201
    assert c.post("/paper-envs", json={"alias": "a", "paper_hash": "h"}).status_code == 409
    assert c.post("/paper-envs/x/env/install", json={"name": "--index-url=x"}).status_code in (404, 422)
    assert c.get("/events/schema").json()["cell_failed"]["required"]


def test_notebook_edit_door_versions_and_revert(api):
    c, core, t = api
    pid = ready_env(c, core, t)
    cell = c.post(f"/paper-envs/{pid}/cells", json={"source": "x = 1", "author": "generator"}).json()
    v = c.put(f"/cells/{cell['id']}/source", json={"source": "x = 2", "author": "agent"}).json()
    assert v["author"] == "agent" and "+x = 2" in v["diff"]
    vs = c.get(f"/cells/{cell['id']}/versions").json()
    assert [x["source"] for x in vs] == ["x = 1", "x = 2"]
    r = c.post(f"/cell-versions/{vs[0]['id']}/revert", json={"author": "human"}).json()
    assert r["source"] == "x = 1" and c.get(f"/cells/{cell['id']}").json()["source"] == "x = 1"
    assert "source" not in c.get(f"/paper-envs/{pid}/cells").json()[0]


def test_run_wait_true_and_async_with_events(api):
    c, core, t = api
    pid = ready_env(c, core, t)
    c.post(f"/paper-envs/{pid}/cells", json={"source": "print('hi')"})
    run = c.post("/runs", json={"paper_env_id": pid, "mode": "clean", "wait": True}).json()
    assert run["status"] == "success" and run["verification_eligible"] is True
    cells = c.get(f"/runs/{run['id']}/cells").json()
    assert cells[0]["outputs"][0]["text"] == "hi\n"
    acc = c.post("/runs", json={"paper_env_id": pid, "mode": "all"})
    assert acc.status_code == 202
    rid = acc.json()["run_id"]
    for _ in range(100):
        r = c.get(f"/runs/{rid}")
        if r.status_code == 200 and r.json()["status"] != "running":
            break
        time.sleep(0.2)
    assert c.get(f"/runs/{rid}").json()["status"] == "success"
    hist = c.get("/events/history", params={"run_id": rid}).json()
    assert [e["type"] for e in hist][0] == "run_started" and hist[-1]["type"] == "run_finished"
    assert len(c.get(f"/paper-envs/{pid}/runs").json()) == 2
    assert c.post("/runs", json={"paper_env_id": pid, "mode": "cell"}).status_code == 422


def test_websocket_replays_then_streams(api):
    c, core, t = api
    pid = ready_env(c, core, t)
    c.post(f"/paper-envs/{pid}/cells", json={"source": "print(1)"})
    first = c.post("/runs", json={"paper_env_id": pid, "mode": "all", "wait": True}).json()
    with c.websocket_connect(f"/events?run_id={first['id']}&after_seq=0") as ws:
        seen = [ws.receive_json() for _ in range(2)]
        assert seen[0]["type"] == "run_started" and seen[0]["seq"] < seen[1]["seq"]
        assert all(e["run_id"] == first["id"] for e in seen)


def test_datasets_over_http(api, tmp_path):
    c, core, _ = api
    f = tmp_path / "d.csv"
    f.write_text("a\n1\n")
    r = c.post("/datasets/upload", json={"name": "d", "version": "v1", "path": str(f)})
    assert r.status_code == 201 and r.json()["status"] == "verified"
    assert c.get("/datasets/d@v1/status").json()["status"] == "verified"
    assert c.post("/datasets/d@v1/verify").json()["ok"] is True
    assert c.get("/datasets/missing@v1").status_code == 404
    reg = {"dataset_id": "u@v1", "name": "u", "version": "v1", "source_type": "url", "source_uri": "http://x/y"}
    assert c.post("/datasets", json=reg).status_code == 201
    assert c.post("/datasets", json={**reg, "source_uri": "http://other/z"}).status_code == 409
    bad = {**reg, "dataset_id": "c@v1", "source_uri": "https://u:secretpass1@h/x"}
    assert c.post("/datasets", json=bad).status_code == 422


def test_blocked_action_resolution_over_http(tmp_path, kernel_template):
    c, core = make_client(tmp_path, kernel_template, hook=Ask())
    try:
        pid = ready_env(c, core, kernel_template)
        cell = c.post(f"/paper-envs/{pid}/cells", json={"source": "print('approved')"}).json()
        run = c.post("/runs", json={"paper_env_id": pid, "mode": "all", "wait": True}).json()
        assert run["status"] == "blocked"
        (blocked,) = c.get("/blocked", params={"paper_env_id": pid}).json()
        assert blocked["kind"] == "execute_cell" and blocked["decision"] == "needs_approval"
        res = c.post(f"/blocked/{blocked['id']}/resolve", json={"decision": "allow", "reason": "ok"}).json()
        assert res["result"]["status"] == "success"
        assert c.get("/blocked").json() == []
        assert c.post(f"/blocked/{blocked['id']}/resolve", json={"decision": "allow"}).status_code == 422
    finally:
        c.__exit__(None, None, None)


def test_checkpoint_resume_over_http(api):
    c, core, t = api
    pid = ready_env(c, core, t)
    c.post(f"/paper-envs/{pid}/cells", json={"source": "v = 5", "declared_outputs": ["v"]})
    c.post(f"/paper-envs/{pid}/cells", json={"source": "print(v)", "declared_inputs": ["v"]})
    run = c.post("/runs", json={"paper_env_id": pid, "mode": "all", "wait": True, "checkpoint": True}).json()
    cps = c.get(f"/paper-envs/{pid}/checkpoints", params={"run_id": run["id"]}).json()
    assert len(cps) == 2 and cps[0]["saved"] == ["v"]
    out = c.post(f"/checkpoints/{cps[0]['id']}/resume", json={"continue_run": True}).json()
    assert out["resume"]["restored"] == ["v"] and out["run"]["status"] == "success"


def test_files_workspace_upload_download_and_confinement(api):
    c, core, t = api
    pid = ready_env(c, core, t, "files-env")
    assert c.put(f"/paper-envs/{pid}/files/data/in.csv", content=b"a,b\n1,2\n").status_code == 201
    assert [f["path"] for f in c.get(f"/paper-envs/{pid}/files").json()] == ["data/in.csv"]
    assert c.get(f"/paper-envs/{pid}/files/data/in.csv").content == b"a,b\n1,2\n"
    for bad in ("../escape.txt", "%2e%2e/escape.txt", "a/../../x"):
        assert c.put(f"/paper-envs/{pid}/files/{bad}", content=b"x").status_code in (404, 422)
    assert not (core.settings.env_dir(pid) / "escape.txt").exists()
    # cells run inside the workspace: uploads are visible, and cell-written files appear in the listing
    c.post(f"/paper-envs/{pid}/cells", json={"source": "print(open('data/in.csv').read().strip().splitlines())\nopen('out.txt','w').write('made by cell')"})
    run = c.post("/runs", json={"paper_env_id": pid, "mode": "all", "wait": True}).json()
    assert run["status"] == "success"
    assert c.get(f"/runs/{run['id']}/cells").json()[0]["outputs"][0]["text"] == "['a,b', '1,2']\n"
    assert c.get(f"/paper-envs/{pid}/files/out.txt").content == b"made by cell"
    assert c.delete(f"/paper-envs/{pid}/files/out.txt").status_code == 204
    assert c.get(f"/paper-envs/{pid}/files/out.txt").status_code == 404


def test_upload_size_cap(api):
    c, core, t = api
    pid = ready_env(c, core, t, "cap-env")
    core.settings.max_upload_bytes = 10
    assert c.put(f"/paper-envs/{pid}/files/big.bin", content=b"x" * 11).status_code == 413
    assert c.get(f"/paper-envs/{pid}/files").json() == []
