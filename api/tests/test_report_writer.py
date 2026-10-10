import json

import pytest
from test_pipeline_api import NO_GAPS, PUBLIC, Stack, parsed
from test_tier2_execution import GOOD, drain, make_worker, start

from crisisgrid_api.agents.report_writer import ReportWriterAgent
from crisisgrid_api.gateway.gateway import Gateway, Provider
from crisisgrid_api.gateway.testing import ScriptedClient
from crisisgrid_core.harness import EscalationRequired, HarnessGuard
from crisisgrid_core.models import Claim, Gap, Source, Verdict

CLAIM = Claim(text="ResNet-20 reaches 91.25% top-1", metric="top1", dataset="cifar10", split="test", reported_value=91.25, unit="percent",
              tolerance=0.5, source=Source(kind="paper", ref="p"), provenance="stated")
VERDICT = Verdict(claim_id=CLAIM.id, status="partial", reported=91.25, measured_mean=90.8, measured_std=0.31, delta=-0.45, n_seeds=3,
                  caveats=["run was scaled down (10% of data)"])
GAPS = [Gap(field="R01:random_seed", severity="minor", description="d", detected_by="rule")]


def writer(*replies):
    client = ScriptedClient(*replies)
    gw = Gateway([Provider("fake", "fake-1", client)], sleep=lambda s: None)
    return ReportWriterAgent(gw, HarnessGuard()), client


def reply(summary, limitations=()):
    return json.dumps({"summary": summary, "limitations": list(limitations)})


def test_a_limitation_with_a_number_not_in_the_evidence_is_rejected():
    w, _ = writer(reply("The claim was partially reproduced: 90.8 measured against 91.25 reported over 3 seeds.", ["Scaled down to 10 percent."]))
    # "10" appears in a caveat but is not a verdict number, so the writer may not repeat it
    with pytest.raises(EscalationRequired):
        w.write([CLAIM], [VERDICT], GAPS)


def test_clean_summary():
    w, client = writer(reply("The claim was partially reproduced: 90.8 measured against 91.25 reported, over 3 seeds, with 1 gap noted.",
                             ["The run was scaled down, so this is not a full reproduction."]))
    out = w.write([CLAIM], [VERDICT], GAPS)
    assert "partially reproduced" in out and "scaled down" in out
    user = client.calls[0][1].content
    assert "90.8" in user and "run was scaled down" in user and "Gap counts" in user


def test_an_invented_number_gets_one_retry_with_the_offending_number_named():
    w, client = writer(reply("It reached 92.4 over 3 seeds."), reply("It reached 90.8 over 3 seeds."))
    assert "90.8" in w.write([CLAIM], [VERDICT], GAPS)
    assert len(client.calls) == 2 and "92.4" in client.calls[1][-1].content and "not in the evidence" in client.calls[1][-1].content


def test_repeated_invented_numbers_escalate():
    w, client = writer(reply("It reached 92.4."), reply("It improved 12%."))
    with pytest.raises(EscalationRequired):
        w.write([CLAIM], [VERDICT], GAPS)
    assert len(client.calls) == 2


def test_rounding_to_the_stated_precision_is_allowed_but_altering_is_not():
    w, _ = writer(reply("Measured about 91 against 91.3 reported, 3 seeds."))
    assert w.write([CLAIM], [VERDICT], GAPS)
    w, _ = writer(reply("Measured 90.9 over 3 seeds."), reply("Measured 90.9 over 3 seeds."))
    with pytest.raises(EscalationRequired):
        w.write([CLAIM], [VERDICT], GAPS)


# ---- inside the pipeline ------------------------------------------------------------------------------------------

def run_with_writer(db, writer_replies):
    s = Stack(db, parsed(PUBLIC), NO_GAPS, *writer_replies)
    s.container.orchestrator.d.narrative = True
    w = make_worker(s)
    pid = s.upload()
    run_id = start(s, pid, GOOD)
    drain(w)
    return s, pid, run_id


def test_pipeline_report_includes_a_checked_summary(migrated_url):
    s, pid, run_id = run_with_writer(migrated_url, [reply("The claim was reproduced across 3 clean runs.")])
    md = s.client.get(f"/reports/{pid}").text
    assert md.index("## Summary") < md.index("## Claims") and "reproduced across 3 clean runs" in md and "checked against" in md
    assert "Deviations from the paper" in md and "exactly what was provided" in md
    assert s.state(run_id)["node"] == "final_gate"


def test_a_writer_that_cannot_stay_consistent_degrades_without_blocking_the_run(migrated_url):
    s, pid, run_id = run_with_writer(migrated_url, [reply("It hit 99.9."), reply("It hit 98.7.")])
    st = s.state(run_id)
    assert (st["status"], st["node"]) == ("waiting_gate", "final_gate")  # not stuck
    md = s.client.get(f"/reports/{pid}").text
    assert "## Summary" not in md and "99.9" not in md and "narrative summary was omitted" in md
    assert "**reproduced**" in md  # the deterministic verdicts are still there
