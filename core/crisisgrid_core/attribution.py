"""Change attribution (system_design 5.3): which edit caused a metric difference between two graph versions.

Honesty rules, in order of importance:
  1. A difference smaller than the seed-to-seed noise is reported as "no attributable effect", never explained.
  2. A change touching one node is attributed directly; several changes need one-at-a-time runs, and whatever those
     do not explain is reported as a residual (interaction) instead of being spread over the edits.
  3. If the run budget cannot cover every one-at-a-time run, the result is flagged `partial`.
  4. Confounds (environment changed, several kinds of node changed, capacity changed together with the architecture) are
     listed next to the number, not hidden.
Only clean, verification-eligible runs are accepted, same as the Claim verifier.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from .graph import GraphDiff, diff, topological_order
from .models import ExperimentGraph, RunResult
from .verifier import IneligibleRunError, _norm

__all__ = ["Contribution", "AttributionResult", "attribute", "confound_flags", "plan_single_runs", "NOISE_K"]

NOISE_K = 2.0  # a difference must exceed K standard errors to count as signal

_ARCH_KEYS = ("arch", "layers", "depth", "width", "hidden", "channels", "heads", "dim", "kernel", "blocks")
_OPT_KEYS = ("lr", "learning_rate", "momentum", "weight_decay", "optimizer", "scheduler", "warmup")
_BUDGET_KEYS = ("epochs", "steps", "batch_size", "iterations")
_DATA_KEYS = ("split", "augment", "normaliz", "transform", "crop", "resize", "dataset", "revision", "preprocess", "tokeniz")


class Contribution(BaseModel):
    node_id: str
    measured: bool  # False: no one-at-a-time run was available (budget)
    delta: float | None = None
    n_runs: int = 0
    significant: bool | None = None


class AttributionResult(BaseModel):
    status: Literal["attributed", "no_attributable_effect", "inconclusive"]
    metric: str
    delta: float | None = None  # mean(B) - mean(A)
    noise_se: float | None = None
    n_a: int = 0
    n_b: int = 0
    changed_nodes: list[str] = Field(default_factory=list)
    contributions: list[Contribution] = Field(default_factory=list)
    residual: float | None = None
    partial: bool = False
    flags: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


def _values(runs: Sequence[RunResult], metric: str, caveats: list[str], label: str) -> list[float]:
    for r in runs:
        if r.mode != "clean" or not r.verification_eligible:
            raise IneligibleRunError(f"run {r.run_id} is not a clean run; only clean runs can attribute a change")
    key = _norm(metric)
    ok = [r for r in runs if r.status in ("ok", "scaled_down") and key in {_norm(k) for k in r.metrics}]
    if any(r.status == "scaled_down" for r in ok):
        caveats.append(f"{label}: some runs were scaled down")
    if any(r.approximate_split for r in ok):
        caveats.append(f"{label}: approximate (non-official) split")
    if len(ok) < len(runs):
        caveats.append(f"{label}: {len(runs) - len(ok)} run(s) failed, were infra failures or lacked the metric and were excluded")
    return [next(v for k, v in r.metrics.items() if _norm(k) == key) for r in ok]


def _se(*groups: Sequence[float]) -> float | None:
    """Standard error of a difference of means; None if any group is too small to estimate variance."""
    if any(len(g) < 2 for g in groups):
        return None
    return math.sqrt(sum(statistics.variance(g) / len(g) for g in groups))


def _significant(delta: float, se: float) -> bool:
    return delta != 0 if se == 0 else abs(delta) > NOISE_K * se


def confound_flags(a: ExperimentGraph, b: ExperimentGraph, d: GraphDiff | None = None) -> list[str]:
    d = d or diff(a, b)
    flags: list[str] = []
    if d.env_changed:
        flags.append("the environment (lockfile digest) changed: library versions are a confound")
    if d.added or d.removed:
        names = ", ".join(d.added + d.removed)
        flags.append(f"nodes were added/removed ({names}): structural change cannot be isolated one node at a time")
    types = {b.node(c.node_id).type for c in d.modified}
    if len(types) > 1:
        flags.append(f"several kinds of node changed together ({', '.join(sorted(types))})")
    concerns: set[str] = set()
    for c in d.modified:
        for key in c.spec_changes:
            lk = key.lower()
            if any(t in lk for t in _ARCH_KEYS):
                concerns.add("architecture")
            if any(t in lk for t in _OPT_KEYS):
                concerns.add("optimization")
            if any(t in lk for t in _BUDGET_KEYS):
                concerns.add("training budget")
            if any(t in lk for t in _DATA_KEYS):
                concerns.add("data/preprocessing")
    if "architecture" in concerns:
        flags.append("the architecture changed: model capacity (parameter count) changed with it, so the effect conflates the two")
    if len(concerns) > 1:
        flags.append(f"edits span several concerns ({', '.join(sorted(concerns))})")
    return flags


def plan_single_runs(a: ExperimentGraph, b: ExperimentGraph, seeds: int, max_runs: int) -> tuple[list[str], bool]:
    """Which directly edited nodes get a one-at-a-time run within the budget (upstream first). Returns (nodes, truncated)."""
    d = diff(a, b)
    order = {n.id: i for i, n in enumerate(topological_order(b))}
    nodes = sorted((c.node_id for c in d.modified), key=lambda n: order[n])
    capacity = max_runs // max(seeds, 1)
    return nodes[:capacity], len(nodes) > capacity


def attribute(a: ExperimentGraph, b: ExperimentGraph, metric: str, runs_a: Sequence[RunResult], runs_b: Sequence[RunResult],
              single_runs: dict[str, Sequence[RunResult]] | None = None) -> AttributionResult:
    """`single_runs[node_id]` = clean runs of version A with ONLY that node's edit applied."""
    d = diff(a, b)
    caveats: list[str] = []
    changed = [c.node_id for c in d.modified]
    res = AttributionResult(status="inconclusive", metric=metric, changed_nodes=changed, flags=confound_flags(a, b, d))
    if d.empty:
        res.caveats = ["the two versions are identical: nothing to attribute"]
        return res

    va, vb = _values(runs_a, metric, caveats, "baseline"), _values(runs_b, metric, caveats, "edited")
    res.n_a, res.n_b, res.caveats = len(va), len(vb), caveats
    if not va or not vb:
        caveats.append("a version has no usable measurement")
        return res
    res.delta = statistics.fmean(vb) - statistics.fmean(va)
    se = _se(va, vb)
    if se is None:
        caveats.append("noise floor unknown: need at least 2 clean runs per version to tell signal from seed noise")
        return res
    res.noise_se = se

    if not _significant(res.delta, se):
        res.status = "no_attributable_effect"
        caveats.append(f"the difference ({res.delta:+.4g}) is within {NOISE_K:g} standard errors ({se:.3g}) of seed noise")
        return res

    structural = bool(d.added or d.removed or d.env_changed)
    if len(changed) == 1 and not structural:
        res.status, res.residual = "attributed", 0.0
        res.contributions = [Contribution(node_id=changed[0], measured=True, delta=res.delta, n_runs=len(vb), significant=True)]
        return res

    single = single_runs or {}
    mean_a = statistics.fmean(va)
    contribs: list[Contribution] = []
    for node in changed:
        vals = _values(single.get(node, ()), metric, caveats, f"edit of {node}") if node in single else []
        if not vals:
            contribs.append(Contribution(node_id=node, measured=False))
            continue
        c = statistics.fmean(vals) - mean_a
        node_se = _se(va, vals)
        contribs.append(Contribution(node_id=node, measured=True, delta=c, n_runs=len(vals),
                                     significant=None if node_se is None else _significant(c, node_se)))
    res.contributions = contribs
    measured = [c for c in contribs if c.measured]
    res.partial = len(measured) < len(contribs)
    res.residual = res.delta - sum(c.delta or 0.0 for c in measured)
    if res.partial:
        caveats.append("run budget did not cover every edit: unmeasured edits are folded into the residual")
    if structural:
        caveats.append("structural/environment changes cannot be applied one at a time; they are part of the residual")
    if any(c.significant for c in measured):
        res.status = "attributed"
        if abs(res.residual) > NOISE_K * se:
            caveats.append(f"the edits do not add up to the total: residual {res.residual:+.4g} is an interaction or unmeasured cause")
    else:
        caveats.append("the total difference is real but no single edit explains it: likely an interaction between edits")
    return res
