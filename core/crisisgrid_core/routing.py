"""Route selector (system_design 3.2): deterministic rules from {claims, gaps, artifact_inventory} -> RouteDecision.

Cases (architecture 6):
  1  dataset missing or private  -> Tier 1, mandatory `data` gate (upload, substitute, or stop)
  3  no usable repo              -> Tier 3 on the missing parts, mandatory `gap` gate (approve the gap list/assumptions)
  2  repo + data available       -> Tier 2, autonomous (gates only on stuck / final)
Data is checked first: without data nothing can be executed, whatever the code situation.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import Claim, Gap
from .papers import ArtifactInventory

__all__ = ["RouteDecision", "select_route"]


class RouteDecision(BaseModel):
    case: Literal[1, 2, 3]
    tier: Literal[1, 2, 3]
    reasons: list[str] = Field(default_factory=list)
    required_gates: list[Literal["data", "gap"]] = Field(default_factory=list)


def select_route(claims: list[Claim], gaps: list[Gap], inventory: ArtifactInventory) -> RouteDecision:
    reasons: list[str] = []
    if not claims:
        reasons.append("no claims were extracted: nothing to verify by execution")

    data_blockers = [g for g in gaps if g.field.startswith("R07:") and g.severity == "blocking"]
    no_data = not inventory.datasets or all(d.access in ("private",) or (d.access == "unknown" and not d.url) for d in inventory.datasets)
    if data_blockers or no_data:
        reasons += [g.description for g in data_blockers] or ["no accessible dataset was found"]
        return RouteDecision(case=1, tier=1, reasons=reasons, required_gates=["data"])

    if inventory.repo is None:
        reasons.append("no code repository: rebuild the missing parts from the paper (results labelled directional)")
        return RouteDecision(case=3, tier=3, reasons=reasons, required_gates=["gap"])

    reasons.append("repo and data are available: execute the authors' code")
    return RouteDecision(case=2, tier=2, reasons=reasons, required_gates=[])
