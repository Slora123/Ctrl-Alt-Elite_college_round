"""The endpoints behind the website's dashboard pages: library, understanding, text, artifacts, history, overview, delete."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pdfgen import make_pdf
from test_pipeline_api import NO_GAPS, PAPER, PUBLIC, REPO, Fetcher, Stack, parsed
from test_tier2_execution import GOOD, drain, make_worker, start

from crisisgrid_api.config import Settings
from crisisgrid_api.container import build_container
from crisisgrid_api.db.base import make_engine
from crisisgrid_api.gateway.gateway import Gateway, Provider
from crisisgrid_api.gateway.testing import ScriptedClient
from crisisgrid_api.main import create_app


@pytest.fixture
def db(migrated_url):
    return migrated_url


def rows(url, table, where=""):
    con = sqlite3.connect(url.replace("sqlite:///", ""))
    try:
        return con.execute(f"select count(*) from {table} {where}").fetchone()[0]
    finally:
        con.close()


# ---- library ------------------------------------------------------------------------------------------------------

def test_library_lists_title_pages_size_and_the_latest_analysis(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    [p] = s.client.get("/papers").json()
    assert (p["id"], p["pages"], p["title"], p["latest_run"]) == (pid, 1, "p", None)  # before analysis: file name, no run
    assert p["size"] == len(PAPER) and p["filename"] == "p.pdf"

    s.run(pid)
    [p] = s.client.get("/papers").json()
    run = p["latest_run"]
    assert p["title"] == "ResNet" and run["status"] == "waiting_gate" and run["node"] == "final_gate"
    assert run["claims"] == 1 and run["gaps"] >= 1 and run["created_at"] and run["updated_at"]
    assert s.client.get(f"/papers/{pid}").json()["title"] == "ResNet"


def test_the_page_count_is_recorded_at_upload(db):
    s = Stack(db, "{}")
    two = make_pdf([["page one text " * 5], ["page two text " * 5]])
    pid = s.upload(two)
    assert s.client.get("/papers").json()[0]["pages"] == 2 and rows(db, "papers", "where pages=2") == 1
    assert pid


# ---- understanding / text / artifacts -----------------------------------------------------------------------------

def test_understanding_before_and_after_the_analysis(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    before = s.client.get(f"/papers/{pid}/understanding").json()
    assert before["parsed"] is None and before["run"] is None and before["llm_configured"] is True
    assert before["paper"]["pages"] == 1 and before["paper"]["chars"] > 50 and before["paper"]["needs_ocr"] is False

    s.run(pid)
    after = s.client.get(f"/papers/{pid}/understanding").json()
    parsed_ = after["parsed"]
    assert parsed_["title"] == "ResNet" and parsed_["claims"][0]["reported_value"] == 91.25
    assert parsed_["claims"][0]["source"]["quote"].startswith("Our model achieves")  # the verified quote travels with the claim
    assert parsed_["hyperparameters"]["learning_rate"]["provenance"] == "stated"
    assert after["run"]["status"] == "waiting_gate"


def test_understanding_flags_a_scanned_pdf_and_a_server_without_an_llm(db):
    s = Stack(db, "{}", llm=False)
    pid = s.upload(make_pdf([[""]]))
    u = s.client.get(f"/papers/{pid}/understanding").json()
    assert u["paper"]["needs_ocr"] is True and u["llm_configured"] is False and u["parsed"] is None
    assert s.client.get(f"/papers/{pid}/text").json()["needs_ocr"] is True


def test_extracted_text_is_served_per_page(db):
    s = Stack(db, "{}")
    pid = s.upload()
    t = s.client.get(f"/papers/{pid}/text").json()
    assert len(t["pages"]) == 1 and "learning rate of 0.1" in t["pages"][0] and t["chars"] == len(t["pages"][0])
    assert s.client.get("/papers/nope/text").status_code == 404


def test_artifacts_are_null_until_the_analysis_has_found_them(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    assert s.client.get(f"/papers/{pid}/artifacts").json() == {"run": None, "inventory": None}
    s.run(pid)
    inv = s.client.get(f"/papers/{pid}/artifacts").json()["inventory"]
    assert inv["repo"]["commit"] == REPO.commit and inv["repo"]["url"] == REPO.url
    assert any(d["name"] == "CIFAR-10" for d in inv["datasets"]) and "missing_components" in inv


# ---- history ------------------------------------------------------------------------------------------------------

def test_history_is_ordered_scoped_to_the_paper_and_limited(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS, parsed(PUBLIC), NO_GAPS)
    a, b = s.upload(), s.upload()
    s.run(a)
    s.run(b)
    h = s.client.get("/history", params={"paper_id": a}).json()
    kinds = [e["kind"] for e in h["events"]]
    assert kinds[0] == "run_started" and "gate_opened" in kinds and {e["paper_id"] for e in h["events"]} == {a}
    assert [e["ts"] for e in h["events"]] == sorted(e["ts"] for e in h["events"]) and h["truncated"] is False
    both = s.client.get("/history").json()["events"]
    assert {e["paper_id"] for e in both} == {a, b} and all(e["paper"] for e in both)
    small = s.client.get("/history", params={"paper_id": a, "limit": 3}).json()
    assert len(small["events"]) == 3 and small["truncated"] is True and small["events"][-1]["id"] == h["events"][-1]["id"]  # newest kept
    assert s.client.get("/history", params={"paper_id": "nope"}).status_code == 404


# ---- overview -----------------------------------------------------------------------------------------------------

def test_overview_of_an_empty_system(db):
    s = Stack(db, "{}")
    o = s.client.get("/overview").json()
    assert o["papers"] == 0 and o["claims"]["total"] == 0 and o["open_gates"] == [] and o["recent_claims"] == []
    assert o["llm_configured"] is True and o["workers"] == {"total": 0, "online": 0}
    assert Stack(db, "{}", llm=False).client.get("/overview").json()["llm_configured"] is False


def test_overview_counts_claims_gaps_gates_and_workers(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    w = make_worker(s)
    run_id = start(s, pid, GOOD)
    o = s.client.get("/overview").json()
    assert o["runs"]["waiting_jobs"] == 1 and o["workers"]["online"] == 1 and o["claims"]["not_executed"] == 1
    drain(w)
    o = s.client.get("/overview").json()
    assert o["stages"] == {"parsed": 1, "audited": 1, "executed": 1}
    assert o["claims"]["reproduced"] == 1 and o["claims"]["not_executed"] == 0 and o["claims"]["total"] == 1
    assert [g["kind"] for g in o["open_gates"]] == ["final"] and o["open_gates"][0]["paper_id"] == pid and o["open_gates"][0]["paper"] == "ResNet"
    assert sum(o["gaps"].values()) >= 1
    [rc] = o["recent_claims"]
    assert rc["status"] == "reproduced" and rc["reported"] == 91.25 and 91.0 <= rc["measured_mean"] <= 91.5 and rc["paper_id"] == pid
    s.answer(s.gate(run_id)["id"], "accept")
    o = s.client.get("/overview").json()
    assert o["open_gates"] == [] and o["runs"]["completed"] == 1


# ---- delete -------------------------------------------------------------------------------------------------------

def test_deleting_a_paper_erases_everything_stored_for_it_and_nothing_else(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS, parsed(PUBLIC), NO_GAPS, sql_audit=True)
    keep, gone = s.upload(), s.upload()
    w = make_worker(s)
    run_gone = start(s, gone, GOOD, seeds=[0])
    drain(w)
    s.run(keep)
    assert s.client.get(f"/papers/{gone}").status_code == 200

    assert s.client.delete(f"/papers/{gone}").status_code == 204
    assert s.client.get(f"/papers/{gone}").status_code == 404
    assert s.client.delete(f"/papers/{gone}").status_code == 404
    assert [p["id"] for p in s.client.get("/papers").json()] == [keep]
    for table, col in (("pipeline_runs", "run_id"), ("gates", "run_id"), ("jobs", "run_id"), ("run_events", "run_id"), ("llm_calls", "run_id")):
        assert rows(db, table, f"where {col}='{run_gone}'") == 0, table  # worker output and prompts (the paper text) are gone too
    assert rows(db, "papers", f"where id='{gone}'") == 0
    assert rows(db, "pipeline_runs") == 1 and rows(db, "gates") == 1 and rows(db, "run_events") > 0 and rows(db, "llm_calls") > 0
    assert s.client.get(f"/papers/{keep}/understanding").json()["parsed"]["title"] == "ResNet"  # the other paper is untouched


def test_a_paper_whose_analysis_is_executing_right_now_cannot_be_deleted(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    run_id = s.run(pid)
    state = s.container.runs.get(run_id)
    state.status = "running"
    s.container.runs.save(state)
    r = s.client.delete(f"/papers/{pid}")
    assert r.status_code == 409 and "running" in r.json()["detail"]
    assert s.client.get(f"/papers/{pid}").status_code == 200


# ---- ownership (authentication ON) --------------------------------------------------------------------------------

@pytest.fixture
def authed(migrated_url):
    settings = Settings(database_url=migrated_url, auth_enabled=True, auth_secret="dash-test-secret-0123456789abcdef",
                        reaper_interval_s=0, paper_env_enabled=False)
    engine = make_engine(migrated_url)
    gw = Gateway([Provider("fake", "fake-1", ScriptedClient(*[parsed(PUBLIC), NO_GAPS] * 4))], sleep=lambda s: None)
    client = TestClient(create_app(settings, engine, build_container(engine, settings, gateway=gw, fetcher=Fetcher())))
    client.__enter__()
    yield client
    client.__exit__(None, None, None)


def login(c, email, name):
    r = c.post("/auth/register", json={"email": email, "name": name, "password": "correct-horse-battery"})
    assert r.status_code == 201, r.text
    return {"authorization": "Bearer " + r.json()["token"]}


def upload_as(c, h):
    r = c.post("/papers", content=PAPER, headers={**h, "content-type": "application/pdf", "x-filename": "mine.pdf"})
    assert r.status_code == 201, r.text
    pid = r.json()["paper_id"]
    assert c.post(f"/papers/{pid}/runs", json={}, headers=h).status_code == 202
    return pid


def test_every_new_endpoint_respects_ownership(authed):
    admin = login(authed, "admin@example.com", "Admin")
    ada = login(authed, "ada@example.com", "Ada")
    bob = login(authed, "bob@example.com", "Bob")
    pid = upload_as(authed, ada)

    assert authed.get("/papers", headers=ada).json()[0]["title"] == "ResNet"
    assert authed.get("/papers", headers=bob).json() == []
    for path in (f"/papers/{pid}/understanding", f"/papers/{pid}/text", f"/papers/{pid}/artifacts", f"/history?paper_id={pid}"):
        assert authed.get(path, headers=ada).status_code == 200, path
        assert authed.get(path, headers=bob).status_code == 404, path     # not 403: no hint that the paper exists
        assert authed.get(path, headers=admin).status_code == 200, path
    assert authed.get("/history", headers=bob).json()["events"] == []
    ob = authed.get("/overview", headers=bob).json()
    assert ob["papers"] == 0 and ob["claims"]["total"] == 0 and ob["open_gates"] == [] and ob["recent_claims"] == []
    assert authed.get("/overview", headers=ada).json()["papers"] == 1
    assert authed.get("/overview", headers=admin).json()["papers"] == 1

    assert authed.delete(f"/papers/{pid}", headers=bob).status_code == 404
    assert authed.get(f"/papers/{pid}", headers=ada).status_code == 200      # Bob's attempt changed nothing
    assert authed.get("/overview").status_code == 401 and authed.delete(f"/papers/{pid}").status_code == 401
    assert authed.delete(f"/papers/{pid}", headers=ada).status_code == 204


def test_a_viewer_can_read_their_paper_but_not_delete_it(authed):
    admin = login(authed, "admin@example.com", "Admin")
    ada = login(authed, "ada@example.com", "Ada")
    pid = upload_as(authed, ada)
    uid = authed.get("/auth/me", headers=ada).json()["id"]
    assert authed.patch(f"/auth/users/{uid}", json={"role": "viewer"}, headers=admin).status_code == 200
    assert authed.get("/papers", headers=ada).status_code == 401           # a role change signs the user out everywhere
    viewer = {"authorization": "Bearer " + authed.post("/auth/login", json={"email": "ada@example.com", "password": "correct-horse-battery"}).json()["token"]}
    assert authed.get(f"/papers/{pid}/understanding", headers=viewer).status_code == 200
    assert authed.get("/overview", headers=viewer).json()["papers"] == 1
    assert authed.delete(f"/papers/{pid}", headers=viewer).status_code == 403
    assert authed.get(f"/papers/{pid}", headers=admin).status_code == 200  # still there
