"""Claim verifier (system_design.md 5.2). Deterministic; no LLM. The only place a verdict is decided.

Only clean runs are evidence. An interactive (or otherwise non-eligible) RunResult is rejected with an error, never
silently dropped: a caller that hands over interactive runs has a bug we want to see.
"""
from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import Any

from .models import Claim, RunResult, Verdict, VerdictStatus

__all__ = ["verify_claim", "IneligibleRunError", "MIN_SEEDS"]

MIN_SEEDS = 3
_USED = ("ok", "scaled_down")


class IneligibleRunError(ValueError):
    """Raised when a run that is not verification-eligible (e.g. interactive) is offered as evidence."""


def verify_claim(claim: Claim, runs: Sequence[RunResult], *, metric_mismatch: str | None = None) -> Verdict:
    """Compare clean-run measurements to the claim.

    `metric_mismatch` is set by the caller when an Audit rule found the run's metric definition differs from the
    claim's (top-1 vs top-5, micro vs macro): the verdict is then `inconclusive`, before any comparison.
    """
    for r in runs:
        if r.mode != "clean" or not r.verification_eligible:
            raise IneligibleRunError(
                f"run {r.run_id} (mode={r.mode}, verification_eligible={r.verification_eligible}) "
                "cannot be used as evidence; only clean runs are verification-eligible"
            )

    caveats: list[str] = []

    def verdict(status: VerdictStatus, **kw: Any) -> Verdict:
        return Verdict(claim_id=claim.id, reported=claim.reported_value, status=status, **kw)

    if not runs:
        return verdict("not_executed", caveats=["no clean run was attempted"])

    infra = [r for r in runs if r.status == "infra_failed"]
    failed = [r for r in runs if r.status in ("failed", "timeout")]
    if infra:
        caveats.append(f"{len(infra)} run(s) infra_failed and excluded (infrastructure problem, not evidence)")
    if failed:
        caveats.append(f"{len(failed)} run(s) failed or timed out and are not counted as measurements")

    if metric_mismatch:
        return verdict("inconclusive", caveats=[*caveats, f"metric definition mismatch: {metric_mismatch}"])

    used = [r for r in runs if r.status in _USED and claim.metric in r.metrics]
    missing = [r for r in runs if r.status in _USED and claim.metric not in r.metrics]
    if missing:
        caveats.append(f"{len(missing)} run(s) did not report metric '{claim.metric}'")
    if not used:
        return verdict("inconclusive", caveats=[*caveats, "no usable measurement"])

    values = [r.metrics[claim.metric] for r in used]
    n = len(values)
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if n > 1 else None
    delta = mean - claim.reported_value

    status: VerdictStatus
    if abs(delta) <= claim.tolerance:
        status = "reproduced"
    elif std is not None and abs(delta) <= claim.tolerance + 2 * std:
        status = "partial"
    else:
        status = "not_reproduced"

    cap = False
    if any(r.status == "scaled_down" for r in used):
        caveats.append("run was scaled down (" + "; ".join(sorted({r.scale_note for r in used
                                                                   if r.status == "scaled_down" and r.scale_note}) or ["no note"]) + ")")
        cap = True
    if any(r.approximate_split for r in used):
        caveats.append("approximate (non-official) data split used")
        cap = True
    if cap and status == "reproduced":
        status = "partial"
        caveats.append("verdict capped at partial")
    if n < MIN_SEEDS:
        caveats.append(f"few seeds (n={n} < {MIN_SEEDS})")

    return verdict(status, measured_mean=mean, measured_std=std, delta=delta, n_seeds=n,
                   evidence_run_ids=[r.run_id for r in used], caveats=caveats)
