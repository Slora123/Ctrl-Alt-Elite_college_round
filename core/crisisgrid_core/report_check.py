"""Report writer safeguards (system_design 4.8): the narrative may only state numbers that exist in the evidence, every agent
edit is listed as a deviation from the paper, and reconstruction fidelity is kept separate from reproducibility so a bad
translation is never blamed on the paper."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from .models import ExperimentGraph, Verdict

__all__ = ["numbers_in", "check_numbers", "allowed_numbers", "Deviation", "Fidelity", "fidelity_summary", "render_deviations",
           "attribution_guidance"]

_NUM = re.compile(r"(?<![\w.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?%?")


def numbers_in(text: str) -> list[tuple[str, float]]:
    """(literal, value) for every number in the text, ignoring code spans and URLs."""
    text = re.sub(r"`[^`]*`|https?://\S+", " ", text)
    out = []
    for m in _NUM.finditer(text):
        lit = m.group(0)
        try:
            out.append((lit, float(lit.rstrip("%").replace(",", ""))))
        except ValueError:
            continue
    return out


def _decimals(lit: str) -> int:
    body = lit.rstrip("%").replace(",", "")
    if "e" in body.lower():
        return 6
    return len(body.split(".")[1]) if "." in body else 0


def check_numbers(text: str, allowed: Iterable[float]) -> list[str]:
    """Literals in `text` that match no allowed value once rounded to the precision the text uses ("91.2" matches 91.25 only
    if 91.25 rounds to it). Returns the offending literals."""
    pool = [float(a) for a in allowed]
    bad = []
    for lit, x in numbers_in(text):
        tol = 0.5 * 10 ** -_decimals(lit) + 1e-12
        if not any(abs(x - v) <= tol for v in pool):
            bad.append(lit)
    return bad


def allowed_numbers(verdicts: Iterable[Verdict], *, extra: Iterable[float] = ()) -> list[float]:
    out: list[float] = list(extra)
    for v in verdicts:
        out += [v.reported, float(v.n_seeds)]
        out += [x for x in (v.measured_mean, v.measured_std, v.delta) if x is not None]
        if v.delta is not None:
            out.append(abs(v.delta))
    return out


class Deviation(BaseModel):
    """An edit made by an agent (not a human): by definition a deviation from what the paper states."""

    node_id: str | None = None
    cell_id: str | None = None
    author: Literal["generator", "agent"] = "agent"
    summary: str
    diff: str | None = None


def render_deviations(devs: list[Deviation]) -> str:
    if not devs:
        return "No agent-authored edits were applied: the executed code is exactly what was provided."
    lines = [f"{len(devs)} agent-authored edit(s) deviate from the paper/authors' code:"]
    for d in devs:
        where = d.node_id or d.cell_id or "notebook"
        lines.append(f"- `{where}` ({d.author}): {d.summary}")
    return "\n".join(lines)


class Fidelity(BaseModel):
    """How faithful the reconstruction is to the paper, independent of whether results match."""

    nodes: int
    stated: int
    inferred: int
    human: int
    agent_edits: int
    score: float  # share of nodes taken directly from the paper (stated) or confirmed by a human
    level: Literal["high", "medium", "low"]
    inferred_nodes: list[str] = Field(default_factory=list)


def fidelity_summary(graph: ExperimentGraph, deviations: list[Deviation] | None = None) -> Fidelity:
    nodes = [n for n in graph.nodes if n.type != "claim"]
    stated = sum(n.provenance == "stated" for n in nodes)
    human = sum(n.provenance == "human" for n in nodes)
    inferred = [n.id for n in nodes if n.provenance == "inferred"]
    total = len(nodes) or 1
    score = (stated + human) / total
    edits = len(deviations or [])
    level: Literal["high", "medium", "low"] = "high" if score >= 0.8 and edits == 0 else "medium" if score >= 0.5 else "low"
    return Fidelity(nodes=len(nodes), stated=stated, inferred=len(inferred), human=human, agent_edits=edits, score=round(score, 3),
                    level=level, inferred_nodes=inferred)


def attribution_guidance(verdict_status: str, fidelity: Fidelity) -> str | None:
    """A failure to reproduce is only evidence about the paper when the reconstruction is faithful."""
    if verdict_status in ("not_reproduced", "partial") and fidelity.level != "high":
        return ("This result cannot be blamed on the paper: the reconstruction has "
                f"{fidelity.level} fidelity ({fidelity.inferred} inferred node(s), {fidelity.agent_edits} agent edit(s)).")
    return None
