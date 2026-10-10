"""Authentication, roles and ownership on the paper_env API (HTTP and WebSocket)."""
import pytest
from fastapi.testclient import TestClient

from paper_env.api import create_api
from paper_env.auth import Principal, StaticTokenAuthenticator
from paper_env.config import Settings
from paper_env.container import build_app
from paper_env.repos.memory import memory_uow_factory

USERS = {
    "t-admin": Principal("u-admin", "Ada", "admin"),
    "t-alice": Principal("u-alice", "Alice", "researcher"),
    "t-bob": Principal("u-bob", "Bob", "researcher"),
    "t-view": Principal("u-view", "Vera", "viewer"),
}


@pytest.fixture
def client(tmp_path):
    core = build_app(Settings(data_dir=tmp_path / "d", reaper_interval_s=0), uow_factory=memory_uow_factory(),
                     authenticator=StaticTokenAuthenticator(USERS), network_check=lambda m: True)
    with TestClient(create_api(core)) as c:
        c.core = core
        yield c


def H(tok):
    return {"authorization": f"Bearer {tok}"}


def mk(c, tok, alias):
    r = c.post("/paper-envs", headers=H(tok), json={"alias": alias, "paper_hash": "h"})
    assert r.status_code == 201, r.text
    return r.json()["paper_env"]["id"]


def test_health_is_public_everything_else_needs_a_token(client):
    assert client.get("/health").status_code == 200
    for path in ("/me", "/paper-envs", "/datasets", "/events/history", "/secrets", "/runtimes", "/blocked"):
        assert client.get(path).status_code == 401, path
    assert client.get("/me", headers=H("wrong")).status_code == 401
    assert client.get("/me", headers=H("t-alice")).json() == {"id": "u-alice", "name": "Alice", "role": "researcher"}


def test_query_token_is_accepted_for_downloads(client):
    pid = mk(client, "t-alice", "e1")
    client.put(f"/paper-envs/{pid}/files/a.txt", headers=H("t-alice"), content=b"hi")
    assert client.get(f"/paper-envs/{pid}/files/a.txt").status_code == 401
    assert client.get(f"/paper-envs/{pid}/files/a.txt?token=t-alice").content == b"hi"


def test_owner_isolation_across_every_kind_of_id(client):
    a = mk(client, "t-alice", "alice-env")
    cell = client.post(f"/paper-envs/{a}/cells", headers=H("t-alice"), json={"source": "x = 1"}).json()
    ver = client.get(f"/cells/{cell['id']}/versions", headers=H("t-alice")).json()[0]
    # bob: not in his list, and every route that reaches alice's data (by env, cell or version id) is refused
    assert client.get("/paper-envs", headers=H("t-bob")).json() == []
    for method, path in [("get", f"/paper-envs/{a}"), ("get", f"/paper-envs/{a}/cells"), ("get", f"/cells/{cell['id']}"),
                         ("get", f"/cells/{cell['id']}/versions"), ("get", f"/paper-envs/{a}/files"),
                         ("get", f"/paper-envs/{a}/export.ipynb"), ("delete", f"/paper-envs/{a}")]:
        assert getattr(client, method)(path, headers=H("t-bob")).status_code == 403, path
    r = client.put(f"/cells/{cell['id']}/source", headers=H("t-bob"), json={"source": "x = 2", "author": "human"})
    assert r.status_code == 403
    assert client.post(f"/cell-versions/{ver['id']}/revert", headers=H("t-bob"), json={}).status_code == 403
    assert client.post("/runs", headers=H("t-bob"), json={"paper_env_id": a, "mode": "all"}).status_code == 403
    # alice and the admin still can
    assert client.get(f"/paper-envs/{a}", headers=H("t-alice")).status_code == 200
    assert client.get(f"/paper-envs/{a}", headers=H("t-admin")).status_code == 200
    assert [e["alias"] for e in client.get("/paper-envs", headers=H("t-admin")).json()] == ["alice-env"]
    assert client.core.paper_envs.get(a).owner_id == "u-alice"


def test_viewer_is_read_only(client):
    a = mk(client, "t-alice", "e")
    assert client.get("/paper-envs", headers=H("t-view")).status_code == 200
    # a viewer cannot write anything, anywhere
    assert client.post("/paper-envs", headers=H("t-view"), json={"alias": "v", "paper_hash": "h"}).status_code == 403
    assert client.put("/secrets/X", headers=H("t-view"), json={"value": "abcdef"}).status_code == 403
    assert client.post("/datasets/upload", headers=H("t-view"), json={"name": "n", "version": "v", "path": "/x"}).status_code == 403
    assert client.get(f"/paper-envs/{a}", headers=H("t-view")).status_code == 403  # nor see others' environments


def test_event_history_is_filtered_per_user(client):
    a, b = mk(client, "t-alice", "ea"), mk(client, "t-bob", "eb")
    client.post(f"/paper-envs/{a}/cells", headers=H("t-alice"), json={"source": "1"})
    client.post(f"/paper-envs/{b}/cells", headers=H("t-bob"), json={"source": "2"})
    seen = {e["paper_env_id"] for e in client.get("/events/history", headers=H("t-alice")).json() if e["paper_env_id"]}
    assert seen == {a}
    assert {e["paper_env_id"] for e in client.get("/events/history", headers=H("t-admin")).json() if e["paper_env_id"]} == {a, b}


def test_websocket_requires_a_valid_token_and_filters_events(client):
    from starlette.websockets import WebSocketDisconnect
    a, b = mk(client, "t-alice", "ea"), mk(client, "t-bob", "eb")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/events"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/events?token=nope"):
            pass
    client.post(f"/paper-envs/{b}/cells", headers=H("t-bob"), json={"source": "secret-of-bob"})
    client.post(f"/paper-envs/{a}/cells", headers=H("t-alice"), json={"source": "mine"})
    with client.websocket_connect("/events?token=t-alice&after_seq=0") as ws:
        got = [ws.receive_json() for _ in range(1)]
    assert all(e["paper_env_id"] in (a, None) for e in got) and got[0]["paper_env_id"] == a
    with pytest.raises(WebSocketDisconnect):  # bob cannot subscribe to alice's environment's terminal either
        with client.websocket_connect(f"/paper-envs/{a}/terminal?token=t-bob"):
            pass


def test_secrets_are_per_user_and_values_never_returned(client):
    client.put("/secrets/API_KEY", headers=H("t-alice"), json={"value": "alice-secret-value-1", "allow_in_cells": True})
    client.put("/secrets/API_KEY", headers=H("t-bob"), json={"value": "bob-secret-value-2"})
    for tok, want in (("t-alice", True), ("t-bob", False)):
        listed = client.get("/secrets", headers=H(tok)).json()
        assert [(s["name"], s["allow_in_cells"]) for s in listed] == [("API_KEY", want)]
        assert "secret-value" not in str(listed)
    assert client.core.secrets_store.granted_to_cells("u-alice") == {"API_KEY": "alice-secret-value-1"}
    assert client.core.secrets_store.granted_to_cells("u-bob") == {}
    assert client.delete("/secrets/API_KEY", headers=H("t-alice")).status_code == 204
    assert client.get("/secrets", headers=H("t-alice")).json() == []
    assert client.get("/secrets", headers=H("t-bob")).json()[0]["name"] == "API_KEY"


def test_anonymous_mode_when_no_authenticator(tmp_path):
    core = build_app(Settings(data_dir=tmp_path / "d", reaper_interval_s=0), uow_factory=memory_uow_factory())
    with TestClient(create_api(core)) as c:
        assert c.get("/me").json()["role"] == "admin"
        assert c.post("/paper-envs", json={"alias": "x", "paper_hash": "h"}).status_code == 201


def test_require_auth_without_authenticator_fails_closed(tmp_path):
    core = build_app(Settings(data_dir=tmp_path / "d", reaper_interval_s=0, require_auth=True), uow_factory=memory_uow_factory())
    with TestClient(create_api(core)) as c:
        assert c.get("/me").status_code == 401
