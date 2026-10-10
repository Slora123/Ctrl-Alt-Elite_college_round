import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pdfgen import make_pdf

from crisisgrid_api.config import Settings
from crisisgrid_api.container import build_container
from crisisgrid_api.db.base import make_engine
from crisisgrid_api.gateway.gateway import Gateway, Provider
from crisisgrid_api.gateway.testing import ScriptedClient
from crisisgrid_api.main import create_app
from crisisgrid_core.papers import RepoInfo

PAPER = make_pdf([[
    "We train ResNet-20 on CIFAR-10 with a learning rate of 0.1 and a batch size of 128 for 160 epochs using SGD.",
    "Our model achieves 91.25% top-1 accuracy on the test set.",
    "Code is available at https://github.com/acme/resnet-cifar.",
]])
REPO = RepoInfo(url="https://github.com/acme/resnet-cifar", commit="abc", default_branch="main",
                default_branch_last_commit=datetime(2020, 1, 1, tzinfo=timezone.utc), dependencies_pinned=True, has_readme=True)


def parsed(datasets):
    return json.dumps({
        "title": "ResNet", "datasets": datasets,
        "claims": [{"text": "91.25 top-1", "metric": "top1", "dataset": "cifar10", "split": "test", "reported_value": 91.25,
                    "unit": "percent", "tolerance": 0.5, "provenance": "stated",
                    "source": {"kind": "paper", "ref": "p1", "quote": "Our model achieves 91.25% top-1 accuracy on the test set."}}],
        "hyperparameters": {"learning_rate": {"value": 0.1, "source": {"kind": "paper", "ref": "p", "quote": "a learning rate of 0.1"}}},
    })


PUBLIC = [{"name": "CIFAR-10", "access": "public", "url": "http://x/cifar", "split_provenance": "paper_official"}]
PRIVATE = [{"name": "Proprietary", "access": "private"}]
NO_GAPS = json.dumps({"gaps": []})


class Fetcher:
    def __init__(self, info=REPO):
        self.info = info

    def fetch(self, url):
        return self.info


class Stack:
    """An app + container on a database file. Build a second Stack on the same file to simulate a server restart."""

    def __init__(self, db_url, *replies, fetcher=None, llm=True):
        self.script = ScriptedClient(*replies)
        engine = make_engine(db_url)
        settings = Settings(database_url=db_url)
        gw = Gateway([Provider("fake", "fake-1", self.script)], sleep=lambda s: None) if llm else None
        self.container = build_container(engine, settings, gateway=gw, fetcher=fetcher or Fetcher())
        self.client = TestClient(create_app(settings, engine, self.container))

    def upload(self, pdf=PAPER):
        r = self.client.post("/papers", content=pdf, headers={"content-type": "application/pdf", "x-filename": "p.pdf"})
        assert r.status_code == 201, r.text
        return r.json()["paper_id"]

    def run(self, paper_id):
        r = self.client.post(f"/papers/{paper_id}/runs")
        assert r.status_code == 202, r.text
        return r.json()["run_id"]  # TestClient finishes background tasks before returning

    def state(self, run_id):
        return self.client.get(f"/runs/{run_id}").json()

    def gate(self, run_id):
        gates = self.client.get("/gates", params={"run_id": run_id}).json()
        assert len(gates) == 1, gates
        return gates[0]

    def answer(self, gate_id, action, **payload):
        return self.client.post(f"/gates/{gate_id}/respond", json={"action": action, "payload": payload})

    def events(self, run_id):
        r = self.client.get(f"/runs/{run_id}/events", params={"follow": "false"})
        return [ln.split(": ", 1)[1] for ln in r.text.splitlines() if ln.startswith("event: ")]


@pytest.fixture
def db(migrated_url):
    return migrated_url


# ---- the three routes ---------------------------------------------------------------------------------------------

def test_case1_pauses_at_data_gate_survives_restart_and_resumes(db):
    s1 = Stack(db, parsed(PRIVATE), NO_GAPS)
    run_id = s1.run(s1.upload())
    st = s1.state(run_id)
    assert (st["status"], st["node"], st["route"]["case"]) == ("waiting_gate", "data_gate", 1)
    g = s1.gate(run_id)
    assert g["kind"] == "data" and set(g["allowed_actions"]) == {"upload", "substitute", "stop_tier1"}
    assert any("Proprietary" in d["name"] for d in g["context"]["datasets"]) or g["context"]["blocking_gaps"]
    calls_before = len(s1.script.calls)

    # --- server restart: brand new engine, container, orchestrator, app; same database
    s2 = Stack(db, "")  # a script with no real replies: nothing after the gate may call the model
    assert s2.state(run_id)["status"] == "waiting_gate"
    assert s2.gate(run_id)["id"] == g["id"]
    assert s2.answer(g["id"], "upload", note="uploaded data").status_code == 200
    st = s2.state(run_id)
    assert (st["status"], st["node"]) == ("waiting_gate", "final_gate")
    assert s2.script.calls == [] and calls_before == 2

    fg = s2.gate(run_id)
    assert fg["kind"] == "final"
    assert s2.answer(fg["id"], "accept").status_code == 200
    assert s2.state(run_id)["status"] == "completed"
    ev = s2.events(run_id)
    assert ev[0] == "run_started" and ev[-1] == "run_finished" and ev.count("gate_opened") == 2 and ev.count("gate_resolved") == 2
    assert "route_selected" in ev


def test_case1_stop_tier1_ends_with_an_honest_report(db):
    s = Stack(db, parsed(PRIVATE), NO_GAPS)
    run_id = s.run(s.upload())
    assert s.answer(s.gate(run_id)["id"], "stop_tier1").status_code == 200
    assert s.answer(s.gate(run_id)["id"], "accept").status_code == 200
    md = s.client.get("/reports/" + s.client.get(f"/runs/{run_id}").json()["paper_id"]).text
    assert "stopped at Tier 1" in md and "No experiment was executed" in md and "91.25" in md and "R07" in md


def test_case2_runs_through_to_the_final_gate_without_other_gates(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    run_id = s.run(s.upload())
    st = s.state(run_id)
    assert (st["status"], st["node"], st["route"]["case"]) == ("waiting_gate", "final_gate", 2)
    assert s.gate(run_id)["kind"] == "final"
    assert s.answer(s.gate(run_id)["id"], "accept").status_code == 200
    assert s.state(run_id)["status"] == "completed"


def test_case3_no_repo_needs_the_gap_gate(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS, fetcher=Fetcher(None))
    run_id = s.run(s.upload())
    g = s.gate(run_id)
    assert g["kind"] == "gap" and "code" in g["context"]["missing_components"]
    assert s.state(run_id)["route"]["case"] == 3
    assert s.answer(g["id"], "reject").status_code == 200
    assert s.state(run_id)["node"] == "final_gate"
    assert any("rejected" in c for c in s.state(run_id)["caveats"])


# ---- stuck gate: harness guard integration ------------------------------------------------------------------------

def test_invalid_model_output_opens_stuck_gate_and_a_hint_resumes(db):
    s = Stack(db, "not json", "still not json", parsed(PUBLIC), NO_GAPS)
    run_id = s.run(s.upload())
    g = s.gate(run_id)
    assert g["kind"] == "stuck" and g["context"]["node"] == "understand" and "invalid structured output" in g["context"]["reason"]
    assert "edit_code" not in g["allowed_actions"] and "retry_with_hint" in g["allowed_actions"]
    assert s.answer(g["id"], "retry_with_hint", hint="The dataset is CIFAR-10").status_code == 200
    assert s.state(run_id)["node"] == "final_gate"
    assert any("The dataset is CIFAR-10" in m.content for call in s.script.calls[2:] for m in call)


def test_same_failure_twice_stops_offering_retry(db):
    s = Stack(db, "bad", "bad", "bad", "bad", "bad", "bad")
    run_id = s.run(s.upload())
    first = s.gate(run_id)
    assert "retry_with_hint" in first["allowed_actions"]
    s.answer(first["id"], "retry_with_hint", hint="try harder")
    second = [g for g in s.client.get("/gates", params={"run_id": run_id}).json()][0]
    assert second["id"] != first["id"] and "retry_with_hint" not in second["allowed_actions"] and "retry_blocked" in second["context"]
    assert s.answer(second["id"], "retry_with_hint").status_code == 422


def test_skip_stage_continues_with_a_caveat_and_abort_fails_the_run(db):
    s = Stack(db, "x", "x")
    run_id = s.run(s.upload())
    assert s.answer(s.gate(run_id)["id"], "skip_stage").status_code == 200
    st = s.state(run_id)
    assert st["node"] == "final_gate" and any("skipped" in c for c in st["caveats"])
    assert s.answer(s.gate(run_id)["id"], "accept").status_code == 200

    s2 = Stack(db, "x", "x")
    r2 = s2.run(s2.upload())
    s2.answer(s2.gate(r2)["id"], "abort")
    st = s2.state(r2)
    assert st["status"] == "failed" and "aborted" in st["error"] and s2.events(r2)[-1] == "run_failed"


def test_edit_caveats_and_request_rerun_at_the_final_gate(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS, parsed(PUBLIC), NO_GAPS)
    run_id = s.run(s.upload())
    s.answer(s.gate(run_id)["id"], "edit_caveats", caveats=["Reviewed by Ana"])
    assert "Reviewed by Ana" in s.state(run_id)["caveats"] and s.state(run_id)["node"] == "final_gate"
    s.answer(s.gate(run_id)["id"], "request_rerun")
    st = s.state(run_id)
    assert (st["status"], st["node"]) == ("waiting_gate", "final_gate") and len(s.script.calls) == 2  # rerun hit the cache


# ---- gate API contract --------------------------------------------------------------------------------------------

def test_gate_api_errors(db):
    s = Stack(db, parsed(PRIVATE), NO_GAPS)
    run_id = s.run(s.upload())
    gid = s.gate(run_id)["id"]
    assert s.answer(gid, "accept").status_code == 422  # not an allowed action for a data gate
    assert s.answer("nope", "upload").status_code == 404
    assert s.answer(gid, "upload").status_code == 200
    assert s.answer(gid, "upload").status_code == 409  # already resolved
    assert s.client.get("/gates", params={"run_id": run_id, "status": "resolved"}).json()[0]["action"] == "upload"


# ---- inputs / misc endpoints --------------------------------------------------------------------------------------

def test_scanned_pdf_skips_the_model_and_says_so(db):
    s = Stack(db, "{}")
    pid = s.upload(make_pdf([[""]]))
    run_id = s.run(pid)
    assert s.script.calls == [] and s.state(run_id)["needs_ocr"] is True and s.state(run_id)["node"] == "final_gate"
    assert "no extractable text" in s.client.get(f"/reports/{pid}").text


def test_upload_validation_and_listing(db):
    s = Stack(db, "{}")
    assert s.client.post("/papers", content=b"junk").status_code == 422
    assert s.client.post("/papers", content=b"").status_code == 422
    pid = s.upload()
    assert [p["id"] for p in s.client.get("/papers").json()] == [pid]
    assert s.client.get(f"/papers/{pid}").json()["runs"] == [] and s.client.get("/papers/nope").status_code == 404
    assert s.client.get(f"/papers/{pid}/claims").status_code == 404  # no run yet


def test_without_an_llm_reading_works_but_runs_are_refused(db):
    s = Stack(db, llm=False)
    pid = s.upload()
    r = s.client.post(f"/papers/{pid}/runs")
    assert r.status_code == 503 and "CG_LLM_CHAIN" in r.json()["detail"]
    assert s.client.get("/papers").status_code == 200


def test_claims_audit_and_report_endpoints_never_invent_verdicts(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    s.run(pid)
    [c] = s.client.get(f"/papers/{pid}/claims").json()
    assert c["claim"]["reported_value"] == 91.25 and c["verdict"]["status"] == "not_executed"
    assert any(g["field"].startswith("R") for g in s.client.get(f"/papers/{pid}/audit").json())
    j = s.client.get(f"/reports/{pid}", params={"format": "json"}).json()
    assert "No experiment was executed" in j["markdown"] and s.client.get(f"/reports/{pid}", params={"format": "pdf"}).status_code == 422


def test_sse_stream_format_and_termination(db):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    run_id = s.run(s.upload())
    s.answer(s.gate(run_id)["id"], "accept")
    with s.client.stream("GET", f"/runs/{run_id}/events") as r:  # follow=true ends by itself once the run is completed
        body = "".join(r.iter_text())
    blocks = [b for b in body.split("\n\n") if b.startswith("id: ")]
    assert blocks[0].splitlines()[1] == "event: run_started" and blocks[-1].splitlines()[1] == "event: run_finished"
    assert json.loads(blocks[0].splitlines()[2][len("data: "):])["paper_id"]
    ids = [b.splitlines()[0][4:] for b in blocks]
    assert ids == sorted(ids)
    after = s.client.get(f"/runs/{run_id}/events", params={"follow": "false", "after": ids[-3]}).text
    assert after.count("event: ") == 2
    assert s.client.get("/runs/nope/events").status_code == 404
