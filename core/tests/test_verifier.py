import statistics

import pytest
from pydantic import ValidationError

from crisisgrid_core.ids import uuid7
from crisisgrid_core.models import Claim, RunResult, Source
from crisisgrid_core.verifier import IneligibleRunError, verify_claim

CLAIM = Claim(text="top-1 is 91.25%", metric="top1", dataset="cifar10", split="test", reported_value=91.25,
              unit="percent", tolerance=0.5, source=Source(kind="paper", ref="p1"), provenance="stated")


def run(value=None, status="ok", seed=0, **kw):
    kw.setdefault("metrics", {} if value is None else {"top1": value})
    return RunResult(run_id=f"r{seed}-{uuid7()[-6:]}", graph_id="g", seed=seed, status=status, mode="clean",
                     verification_eligible=True, **kw)


def runs(values, **kw):
    return [run(v, seed=i, **kw) for i, v in enumerate(values)]


# value lists, expected status, expected caveat fragments
@pytest.mark.parametrize("values,status", [
    ([91.0, 91.4, 91.3], "reproduced"),
    ([91.25], "reproduced"),            # single seed: within tolerance
    ([91.75, 90.75], "reproduced"),     # boundary: |mean-reported| == 0 -> within tolerance
    ([89.6, 90.0, 90.4], "partial"),    # outside tolerance, inside tolerance + 2*std
    ([85.0, 85.1, 84.9], "not_reproduced"),
    ([89.0], "not_reproduced"),         # no std -> cannot be partial
])
def test_status_table(values, status):
    assert verify_claim(CLAIM, runs(values)).status == status


def test_numbers_and_evidence():
    rs = runs([90.0, 91.0, 92.0])
    v = verify_claim(CLAIM, rs)
    assert v.measured_mean == pytest.approx(91.0)
    assert v.measured_std == pytest.approx(statistics.stdev([90, 91, 92]))
    assert v.delta == pytest.approx(-0.25)
    assert v.n_seeds == 3 and v.caveats == []
    assert v.evidence_run_ids == [r.run_id for r in rs]
    assert (v.claim_id, v.reported) == (CLAIM.id, 91.25)


def test_tolerance_boundary_inclusive():
    assert verify_claim(CLAIM, runs([91.75])).status == "reproduced"
    assert verify_claim(CLAIM, runs([91.76])).status != "reproduced"


def test_few_seeds_caveat():
    assert any("few seeds" in c for c in verify_claim(CLAIM, runs([91.2, 91.3])).caveats)
    assert not any("few seeds" in c for c in verify_claim(CLAIM, runs([91.2, 91.3, 91.25])).caveats)


def test_interactive_run_is_rejected():
    interactive = RunResult(run_id="i", graph_id="g", seed=0, status="ok", mode="interactive",
                            verification_eligible=False, metrics={"top1": 91.25})
    with pytest.raises(IneligibleRunError):
        verify_claim(CLAIM, [interactive])
    with pytest.raises(IneligibleRunError):  # mixed in with good runs: still an error, never silently dropped
        verify_claim(CLAIM, [*runs([91.25, 91.2, 91.3]), interactive])


def test_interactive_run_cannot_claim_eligibility():
    with pytest.raises(ValidationError):
        RunResult(run_id="i", graph_id="g", seed=0, status="ok", mode="interactive", verification_eligible=True)


def test_clean_run_flagged_ineligible_is_rejected():
    r = RunResult(run_id="c", graph_id="g", seed=0, status="ok", mode="clean", verification_eligible=False,
                  metrics={"top1": 91.25})
    with pytest.raises(IneligibleRunError):
        verify_claim(CLAIM, [r])


def test_no_runs_is_not_executed():
    v = verify_claim(CLAIM, [])
    assert v.status == "not_executed" and v.n_seeds == 0 and v.measured_mean is None


@pytest.mark.parametrize("statuses", [["failed"], ["timeout", "failed"], ["infra_failed"], ["infra_failed", "failed"]])
def test_all_attempts_failed_is_inconclusive(statuses):
    v = verify_claim(CLAIM, [run(None, status=s, seed=i) for i, s in enumerate(statuses)])
    assert v.status == "inconclusive" and v.n_seeds == 0 and v.evidence_run_ids == []


def test_infra_failed_excluded_with_caveat():
    rs = [*runs([91.2, 91.3, 91.25]), run(None, status="infra_failed", seed=9, metrics={"top1": 0.0})]
    v = verify_claim(CLAIM, rs)
    assert v.status == "reproduced" and v.n_seeds == 3
    assert any("infra_failed" in c for c in v.caveats)
    assert all("r9" not in rid for rid in v.evidence_run_ids)


def test_failed_run_not_counted_even_with_metrics():
    rs = [*runs([91.2, 91.3, 91.25]), run(None, status="failed", seed=9, metrics={"top1": 0.0})]
    v = verify_claim(CLAIM, rs)
    assert v.n_seeds == 3 and v.status == "reproduced"


def test_scaled_down_caps_at_partial():
    v = verify_claim(CLAIM, runs([91.2, 91.3, 91.25], status="scaled_down", scale_note="10% of data"))
    assert v.status == "partial"
    assert any("scaled down" in c and "10% of data" in c for c in v.caveats)


def test_scaled_down_does_not_upgrade_failure():
    assert verify_claim(CLAIM, runs([80.0, 80.1, 79.9], status="scaled_down")).status == "not_reproduced"


def test_approximate_split_caps_at_partial():
    v = verify_claim(CLAIM, runs([91.2, 91.3, 91.25], approximate_split=True))
    assert v.status == "partial" and any("approximate" in c for c in v.caveats)


def test_metric_mismatch_is_inconclusive_before_comparison():
    v = verify_claim(CLAIM, runs([91.25, 91.25, 91.25]), metric_mismatch="claim is top-5, run measured top-1")
    assert v.status == "inconclusive" and any("top-5" in c for c in v.caveats)


def test_missing_metric_is_inconclusive():
    r = run(None, metrics={"other": 1.0})
    v = verify_claim(CLAIM, [r])
    assert v.status == "inconclusive" and any("did not report" in c for c in v.caveats)


def test_partial_missing_metric_still_uses_the_rest():
    rs = [*runs([91.2, 91.3, 91.25]), run(None, seed=7, metrics={"other": 1.0})]
    v = verify_claim(CLAIM, rs)
    assert v.n_seeds == 3 and any("did not report" in c for c in v.caveats)


def test_uuid7():
    a, b = uuid7(), uuid7()
    assert a != b and a[14] == "7" and a[19] in "89ab" and len(a) == 36
