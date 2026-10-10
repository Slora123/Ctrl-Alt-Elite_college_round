"""User accounts, sessions, API tokens, roles and ownership on the CrisisGrid API (authentication switched ON)."""
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from pdfgen import make_pdf

from crisisgrid_api.auth.security import (
    check_password_policy, hash_api_token, hash_password, normalize_email, sign_jwt, verify_jwt, verify_password,
    PasswordPolicyError,
)
from crisisgrid_api.config import Settings
from crisisgrid_api.main import create_app

SECRET = "unit-test-secret-0123456789abcdef"
PDF = make_pdf([["Some paper text about a model with a learning rate of 0.1."]])
PW = "correct-horse-battery"


@pytest.fixture
def make_client(migrated_url):
    made = []

    def make(**kw):
        s = Settings(database_url=migrated_url, auth_enabled=True, auth_secret=SECRET, reaper_interval_s=0, paper_env_enabled=False, **kw)
        c = TestClient(create_app(s))
        c.__enter__()
        made.append(c)
        return c
    yield make
    for c in made:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def reg(c, email, name="N", pw=PW):
    r = c.post("/auth/register", json={"email": email, "name": name, "password": pw})
    assert r.status_code == 201, r.text
    return r.json()


def H(tok):
    return {"authorization": f"Bearer {tok}"}


# ------------------------------------------------------------------ primitives
def test_password_hashing_is_salted_and_verifies():
    a, b = hash_password(PW), hash_password(PW)
    assert a != b and a.startswith("scrypt$") and verify_password(PW, a) and verify_password(PW, b)
    assert not verify_password("wrong", a) and not verify_password(PW, "garbage") and not verify_password(PW, "bcrypt$x$y")


@pytest.mark.parametrize("pw", ["short", "x" * 257, "password123", "1234567890"])
def test_password_policy_rejects_weak_passwords(pw):
    with pytest.raises(PasswordPolicyError):
        check_password_policy(pw)
    check_password_policy(PW)


def test_email_normalisation():
    assert normalize_email("  Ada@Example.COM ") == "ada@example.com"
    for bad in ("", "no-at", "a@b", "a b@c.de", "x" * 300 + "@a.bc"):
        with pytest.raises(ValueError):
            normalize_email(bad)


def test_jwt_roundtrip_and_every_kind_of_tampering():
    now = time.time()
    good = {"iss": "crisisgrid", "sub": "u1", "exp": now + 60}
    t = sign_jwt(good, SECRET)
    assert verify_jwt(t, SECRET)["sub"] == "u1"
    assert verify_jwt(t, "another-secret-0123456789") is None                    # wrong key
    assert verify_jwt(sign_jwt({**good, "exp": now - 1}, SECRET), SECRET) is None  # expired
    assert verify_jwt(sign_jwt({**good, "iss": "other"}, SECRET), SECRET) is None  # wrong issuer
    assert verify_jwt(sign_jwt({**good, "nbf": now + 3600}, SECRET), SECRET) is None  # not yet valid
    h, b, s = t.split(".")
    none_alg = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    assert verify_jwt(f"{none_alg}.{b}.", SECRET) is None and verify_jwt(f"{none_alg}.{b}.{s}", SECRET) is None  # alg=none attack
    forged = base64.urlsafe_b64encode(json.dumps({**good, "sub": "admin"}).encode()).rstrip(b"=").decode()
    assert verify_jwt(f"{h}.{forged}.{s}", SECRET) is None                      # payload swapped, signature kept
    hs512 = base64.urlsafe_b64encode(json.dumps({"alg": "HS512"}).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f"{hs512}.{b}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert verify_jwt(f"{hs512}.{b}.{sig}", SECRET) is None                     # only HS256 accepted
    for junk in ("", "a.b", "a.b.c.d", "not a token", "..", "e30.e30.e30"):
        assert verify_jwt(junk, SECRET) is None


def test_api_token_hash_is_stable_and_not_reversible_text():
    assert hash_api_token("cgt_abc") == hash_api_token("cgt_abc") != hash_api_token("cgt_abd") and "cgt_abc" not in hash_api_token("cgt_abc")


# ------------------------------------------------------------------ registration and login
def test_first_account_is_admin_later_ones_are_researchers(client):
    cfg = client.get("/auth/config").json()
    assert cfg["auth_enabled"] and cfg["first_user_becomes_admin"]
    a = reg(client, "ada@example.com", "Ada")
    b = reg(client, "bob@example.com", "Bob")
    assert (a["user"]["role"], b["user"]["role"]) == ("admin", "researcher")
    assert client.get("/auth/config").json()["first_user_becomes_admin"] is False
    assert "password" not in json.dumps(a)
    me = client.get("/auth/me", headers=H(b["token"])).json()
    assert me["email"] == "bob@example.com" and me["role"] == "researcher"


def test_duplicate_and_invalid_registration(client):
    reg(client, "ada@example.com")
    assert client.post("/auth/register", json={"email": "ADA@example.com", "name": "x", "password": PW}).status_code == 409
    assert client.post("/auth/register", json={"email": "bad", "name": "x", "password": PW}).status_code == 422
    assert client.post("/auth/register", json={"email": "c@example.com", "name": "x", "password": "short"}).status_code == 422


def test_registration_can_be_closed_but_not_for_the_first_account(make_client):
    c = make_client(allow_registration=False)
    reg(c, "first@example.com")
    r = c.post("/auth/register", json={"email": "second@example.com", "name": "x", "password": PW})
    assert r.status_code == 403
    assert c.get("/auth/config").json()["registration_open"] is False


def test_login_success_and_uniform_failure_messages(client):
    reg(client, "ada@example.com")
    ok = client.post("/auth/login", json={"email": "Ada@Example.com", "password": PW})
    assert ok.status_code == 200 and ok.json()["token"] and ok.json()["user"]["email"] == "ada@example.com"
    wrong = client.post("/auth/login", json={"email": "ada@example.com", "password": "nope-nope-nope"})
    unknown = client.post("/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()  # no user enumeration
    assert client.get("/auth/users", headers=H(ok.json()["token"])).json()[0]["last_login_at"]


def test_lockout_after_repeated_failures_then_recovery(make_client):
    c = make_client(login_max_failures=3, lockout_s=1)
    reg(c, "ada@example.com")
    for _ in range(3):
        assert c.post("/auth/login", json={"email": "ada@example.com", "password": "bad-bad-bad-bad"}).status_code == 401
    locked = c.post("/auth/login", json={"email": "ada@example.com", "password": PW})
    assert locked.status_code == 429 and "locked" in locked.json()["detail"]  # even the right password is refused while locked
    time.sleep(1.2)
    assert c.post("/auth/login", json={"email": "ada@example.com", "password": PW}).status_code == 200


def test_login_rate_limit_per_ip(client):
    codes = [client.post("/auth/login", json={"email": "x@example.com", "password": "whatever-123"}).status_code for _ in range(25)]
    assert 429 in codes and codes[0] == 401


# ------------------------------------------------------------------ tokens and sessions
def test_protected_endpoints_need_a_valid_token(client):
    tok = reg(client, "ada@example.com")["token"]
    for path in ("/papers", "/gates", "/jobs", "/workers", "/auth/me", "/auth/tokens", "/auth/users"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=H("garbage")).status_code == 401, path
    assert client.get("/papers", headers=H(tok)).status_code == 200
    assert client.get("/health").status_code == 200 and client.get("/auth/config").status_code == 200  # public


def test_password_change_invalidates_old_sessions(client):
    s = reg(client, "ada@example.com")
    old = s["token"]
    bad = client.post("/auth/change-password", headers=H(old), json={"current_password": "wrong-wrong-1", "new_password": "a-brand-new-pass"})
    assert bad.status_code == 403
    weak = client.post("/auth/change-password", headers=H(old), json={"current_password": PW, "new_password": "short"})
    assert weak.status_code == 422
    new = client.post("/auth/change-password", headers=H(old), json={"current_password": PW, "new_password": "a-brand-new-pass"}).json()["token"]
    assert client.get("/auth/me", headers=H(old)).status_code == 401  # revoked
    assert client.get("/auth/me", headers=H(new)).status_code == 200
    assert client.post("/auth/login", json={"email": "ada@example.com", "password": PW}).status_code == 401
    assert client.post("/auth/login", json={"email": "ada@example.com", "password": "a-brand-new-pass"}).status_code == 200


def test_logout_all_revokes_session_tokens_but_not_api_tokens(client):
    tok = reg(client, "ada@example.com")["token"]
    api_tok = client.post("/auth/tokens", headers=H(tok), json={"name": "ci"}).json()["token"]
    assert client.post("/auth/logout-all", headers=H(tok)).status_code == 204
    assert client.get("/auth/me", headers=H(tok)).status_code == 401
    assert client.get("/auth/me", headers=H(api_tok)).status_code == 200


def test_api_tokens_create_use_list_revoke(client):
    tok = reg(client, "ada@example.com")["token"]
    created = client.post("/auth/tokens", headers=H(tok), json={"name": "laptop", "expires_days": 1}).json()
    plain = created["token"]
    assert plain.startswith("cgt_") and created["prefix"] == plain[:8] and created["expires_at"]
    assert client.get("/papers", headers=H(plain)).status_code == 200  # works wherever a session token does
    listed = client.get("/auth/tokens", headers=H(tok)).json()
    assert listed[0]["last_used_at"] and plain not in json.dumps(listed)  # the secret is never listed again
    assert client.delete(f"/auth/tokens/{created['id']}", headers=H(tok)).status_code == 204
    assert client.get("/papers", headers=H(plain)).status_code == 401
    assert client.delete(f"/auth/tokens/{created['id']}", headers=H(tok)).status_code in (204, 404)
    assert client.delete("/auth/tokens/does-not-exist", headers=H(tok)).status_code == 404
    assert client.post("/auth/tokens", headers=H(tok), json={"name": ""}).status_code == 422


def test_other_users_cannot_revoke_my_token(client):
    a, b = reg(client, "ada@example.com")["token"], reg(client, "bob@example.com")["token"]
    tid = client.post("/auth/tokens", headers=H(a), json={"name": "mine"}).json()["id"]
    assert client.delete(f"/auth/tokens/{tid}", headers=H(b)).status_code == 404
    assert client.get("/auth/tokens", headers=H(b)).json() == []


def test_expired_session_token_is_rejected(make_client):
    c = make_client(token_ttl_s=1)
    tok = reg(c, "ada@example.com")["token"]
    assert c.get("/auth/me", headers=H(tok)).status_code == 200
    time.sleep(2.2)
    assert c.get("/auth/me", headers=H(tok)).status_code == 401


# ------------------------------------------------------------------ roles and administration
def test_admin_manages_users_and_the_last_admin_is_protected(client):
    a = reg(client, "ada@example.com")
    b = reg(client, "bob@example.com")
    assert client.get("/auth/users", headers=H(b["token"])).status_code == 403
    assert client.patch(f"/auth/users/{b['user']['id']}", headers=H(b["token"]), json={"role": "admin"}).status_code == 403
    users = client.get("/auth/users", headers=H(a["token"])).json()
    assert [u["email"] for u in users] == ["ada@example.com", "bob@example.com"]
    # the only admin cannot demote or disable themselves
    me = a["user"]["id"]
    assert client.patch(f"/auth/users/{me}", headers=H(a["token"]), json={"role": "viewer"}).status_code == 409
    assert client.patch(f"/auth/users/{me}", headers=H(a["token"]), json={"disabled": True}).status_code == 409
    assert client.patch(f"/auth/users/{b['user']['id']}", headers=H(a["token"]), json={"role": "nonsense"}).status_code == 422
    assert client.patch("/auth/users/nope", headers=H(a["token"]), json={"role": "viewer"}).status_code == 404
    # promote bob; now ada may step down
    assert client.patch(f"/auth/users/{b['user']['id']}", headers=H(a["token"]), json={"role": "admin"}).status_code == 200
    assert client.patch(f"/auth/users/{me}", headers=H(a["token"]), json={"role": "researcher"}).status_code == 200


def test_role_change_and_disable_take_effect_immediately(client):
    a = reg(client, "ada@example.com")
    b = reg(client, "bob@example.com")
    bid = b["user"]["id"]
    client.patch(f"/auth/users/{bid}", headers=H(a["token"]), json={"role": "viewer"})
    assert client.get("/auth/me", headers=H(b["token"])).status_code == 401  # old token carried the old role: revoked
    vt = client.post("/auth/login", json={"email": "bob@example.com", "password": PW}).json()["token"]
    assert client.get("/papers", headers=H(vt)).status_code == 200
    assert client.post("/papers", headers={**H(vt), "content-type": "application/pdf"}, content=PDF).status_code == 403  # viewers are read-only
    assert client.post("/auth/tokens", headers=H(vt), json={"name": "x"}).status_code == 403
    client.patch(f"/auth/users/{bid}", headers=H(a["token"]), json={"disabled": True})
    assert client.get("/papers", headers=H(vt)).status_code == 401
    assert client.post("/auth/login", json={"email": "bob@example.com", "password": PW}).status_code == 403
    client.patch(f"/auth/users/{bid}", headers=H(a["token"]), json={"disabled": False})
    assert client.post("/auth/login", json={"email": "bob@example.com", "password": PW}).status_code == 200


# ------------------------------------------------------------------ ownership
def upload(c, tok, name="p.pdf"):
    r = c.post("/papers", content=PDF, headers={**H(tok), "content-type": "application/pdf", "x-filename": name})
    assert r.status_code == 201, r.text
    return r.json()["paper_id"]


def test_users_only_see_their_own_papers_admin_sees_all(client):
    a = reg(client, "ada@example.com")["token"]       # admin
    b = reg(client, "bob@example.com")["token"]
    c_ = reg(client, "cy@example.com")["token"]
    pb = upload(client, b, "bobs.pdf")
    assert [p["id"] for p in client.get("/papers", headers=H(b)).json()] == [pb]
    assert client.get("/papers", headers=H(c_)).json() == []
    assert [p["id"] for p in client.get("/papers", headers=H(a)).json()] == [pb]  # admin
    assert client.get(f"/papers/{pb}", headers=H(b)).status_code == 200
    for path in (f"/papers/{pb}", f"/papers/{pb}/claims", f"/papers/{pb}/audit", f"/reports/{pb}"):
        assert client.get(path, headers=H(c_)).status_code == 404, path  # 404, not 403: existence is not revealed
    assert client.post(f"/papers/{pb}/runs", headers=H(c_), json={}).status_code == 404
    assert client.get(f"/papers/{pb}", headers=H(a)).status_code == 200


def test_runs_gates_and_jobs_follow_paper_ownership(make_client):
    # no LLM is configured, so runs cannot start; seed pipeline state directly through the container instead
    c = make_client()
    a, b = reg(c, "ada@example.com"), reg(c, "bob@example.com")
    cont = c.app.state.container
    pa = upload(c, a["token"])
    from crisisgrid_api.orchestration.models import Gate, RunState
    st = RunState(paper_id=pa, paper_hash="h")
    cont.runs.save(st)
    cont.gate_repo.create(Gate(run_id=st.run_id, kind="data", status="open", context={"q": 1}, allowed_actions=["ok"]))
    assert c.get(f"/runs/{st.run_id}", headers=H(a["token"])).status_code == 200
    assert c.get(f"/runs/{st.run_id}", headers=H(b["token"])).status_code == 404
    assert c.get(f"/runs/{st.run_id}/events", headers=H(b["token"])).status_code == 404
    assert [g["run_id"] for g in c.get("/gates", headers=H(a["token"])).json()] == [st.run_id]
    assert c.get("/gates", headers=H(b["token"])).json() == []
    gid = cont.gate_repo.list(run_id=st.run_id)[0].id
    assert c.post(f"/gates/{gid}/respond", headers=H(b["token"]), json={"action": "ok"}).status_code == 404
    assert c.post("/gates/nope/respond", headers=H(b["token"]), json={"action": "ok"}).status_code == 404


def test_sse_stream_accepts_a_query_token_but_other_routes_do_not(make_client):
    c = make_client()
    a = reg(c, "ada@example.com")["token"]
    pa = upload(c, a)
    from crisisgrid_api.orchestration.models import RunState
    st = RunState(paper_id=pa, paper_hash="h", status="completed")
    c.app.state.container.runs.save(st)
    assert c.get(f"/runs/{st.run_id}/events?follow=false").status_code == 401
    assert c.get(f"/runs/{st.run_id}/events?follow=false&token={a}").status_code == 200
    assert c.get(f"/runs/{st.run_id}?token={a}").status_code == 401   # a token in the URL is accepted nowhere else
    assert c.get(f"/papers?token={a}").status_code == 401


def test_workers_still_use_their_own_tokens(client):
    r = client.post("/workers/register", json={"name": "w"})  # dev mode: open registration, no user login needed
    assert r.status_code == 201
    wtok = r.json()["token"]
    assert client.post("/workers/me/heartbeat", headers=H(wtok)).status_code == 200
    assert client.post("/workers/me/heartbeat", headers=H("cgw_wrong")).status_code == 401
    tok = reg(client, "ada@example.com")["token"]
    assert client.post("/workers/me/heartbeat", headers=H(tok)).status_code == 401  # a user token is not a worker token
    assert client.get("/workers", headers=H(tok)).status_code == 200 and client.get("/workers").status_code == 401


# ------------------------------------------------------------------ auth switched off, and secret handling
def test_auth_disabled_runs_as_local_admin(migrated_url):
    s = Settings(database_url=migrated_url, auth_enabled=False, reaper_interval_s=0, paper_env_enabled=False)
    with TestClient(create_app(s)) as c:
        assert c.get("/auth/config").json()["auth_enabled"] is False
        assert c.get("/papers").status_code == 200
        pid = c.post("/papers", content=PDF, headers={"content-type": "application/pdf"}).json()["paper_id"]
        assert c.app.state.container.papers.get(pid).owner_id is None  # nothing is attributed to a fake user


def test_secret_requirements(tmp_path, migrated_url):
    from crisisgrid_api.auth.service import resolve_secret
    with pytest.raises(RuntimeError, match="at least 16"):
        resolve_secret(Settings(auth_secret="short"))
    with pytest.raises(RuntimeError, match="must be set"):
        resolve_secret(Settings(env="prod", data_dir=str(tmp_path)))
    s1 = resolve_secret(Settings(env="dev", data_dir=str(tmp_path)))
    s2 = resolve_secret(Settings(env="dev", data_dir=str(tmp_path)))
    assert s1 == s2 and len(s1) >= 32  # generated once, then stable across restarts
    assert oct((tmp_path / ".cg_auth_secret").stat().st_mode & 0o777) == "0o600"
