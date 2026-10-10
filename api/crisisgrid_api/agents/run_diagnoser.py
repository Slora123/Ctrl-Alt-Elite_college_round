"""Run diagnoser: why did a clean run fail, and is there a safe fix? It repairs executability, never results.

Firewall: its deps never contain a claim or reported value (HarnessGuard.check_agent_deps enforces it). Tracebacks and cell
source reach the model only as quoted data blocks.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from crisisgrid_core.harness import HarnessGuard

from ..gateway.gateway import Gateway
from ..gateway.prompts import SYSTEM_RULES, untrusted_block
from ..gateway.structured import run_structured
from ..gateway.types import Message

Category = Literal["infra", "dependency", "data_path", "api_change", "bug", "semantic"]

INSTRUCTIONS = """You diagnose why a notebook cell failed in a clean run and, if safe, propose a minimal fix.
Categories: infra (the machine failed: OOM, killed, network), dependency (a package is missing or the wrong version),
data_path (a file or directory is wrong), api_change (a library function was renamed or moved), bug (a plain code error),
semantic (the paper is ambiguous or a value is missing: only a human can decide).
Rules:
- You repair EXECUTABILITY only. Never change hyperparameters, the model, the loss, the data split or preprocessing to make
  numbers look better, and never guess what the result should be.
- A fix is the full new source of ONE cell: {"cell_index": <int>, "new_source": "<python>"}. Keep every other line unchanged.
- infra problems are retried, not patched: give no fix. semantic problems always set needs_human=true.
- dependency problems: list the package requirements in "install" (e.g. "torchvision==0.16.0"); give no code fix.
- If you are not sure, set needs_human=true. Reply as JSON for the given schema."""


class CellFix(BaseModel):
    cell_index: int
    new_source: str


class Diagnosis(BaseModel):
    category: Category
    explanation: str
    fix: CellFix | None = None
    install: list[str] = Field(default_factory=list)
    needs_human: bool = False


class RunDiagnoserAgent:
    def __init__(self, gateway: Gateway, guard: HarnessGuard) -> None:
        self.gateway, self.guard = gateway, guard

    def diagnose(self, error: dict[str, Any], cells: list[dict[str, str]], *, run_id: str | None = None,
                 hint: str | None = None) -> Diagnosis:
        deps = {"error": error, "cells": cells}
        self.guard.check_agent_deps("run_diagnoser", deps)  # raises FirewallViolation if a claim/target ever sneaks in
        numbered = "\n\n".join(f"# --- cell {i} ({c['kind']}) ---\n{c['source']}" for i, c in enumerate(cells))
        tb = f"{error.get('type')}: {error.get('message')}\ncell_index={error.get('cell_index')}\n{error.get('traceback', '')}"
        msgs = [Message(role="system", content=SYSTEM_RULES + "\n" + INSTRUCTIONS),
                Message(role="user", content=untrusted_block("traceback", tb)),
                Message(role="user", content=untrusted_block("notebook cells", numbered))]
        if hint:
            msgs.append(Message(role="user", content=f"Reviewer hint (from the human operator): {hint}"))
        self.guard.start_stage("diagnose")
        d = run_structured(self.gateway, self.guard, "diagnose", "run_diagnoser", msgs, Diagnosis, run_id=run_id)
        return self._sanitize(d, cells)

    @staticmethod
    def _sanitize(d: Diagnosis, cells: list[dict[str, str]]) -> Diagnosis:
        """Deterministic rules the model cannot talk its way around."""
        if d.category == "semantic":
            d.needs_human, d.fix = True, None  # only a human resolves an ambiguous paper
        if d.category == "infra":
            d.fix = None  # infrastructure failures are retried, never patched
        if d.category == "dependency":
            d.fix = None  # installs go through the Env manager, not through code edits
        if d.fix is not None:
            ok = 0 <= d.fix.cell_index < len(cells) and cells[d.fix.cell_index]["kind"] == "code"
            if not ok or d.fix.new_source == cells[d.fix.cell_index]["source"]:
                d.fix, d.needs_human = None, True
        return d
