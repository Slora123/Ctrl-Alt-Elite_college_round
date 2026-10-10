"""Harness guard (architecture 9, system_design 3.4): bounded agents, loop detection, repair firewall.

Pure and deterministic; the clock is injectable so tests never sleep.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
import time
from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel

from .models import Claim

__all__ = [
    "HarnessConfig", "GuardDecision", "HarnessGuard", "FirewallViolation", "loop_key", "normalize_error",
    "assert_no_targets", "EXECUTOR_AGENTS",
]

# Agents that write or run code. They must never see a claim target (principle 10).
EXECUTOR_AGENTS = frozenset({"code_rebuilder", "run_diagnoser", "env_builder"})
_TARGET_KEYS = frozenset({"reported_value", "reported", "target", "target_value", "claim", "claims", "baseline_value"})


class FirewallViolation(RuntimeError):
    pass


def _walk(obj: Any, path: str, seen: set[int]) -> Iterable[tuple[str, Any]]:
    if id(obj) in seen:
        return
    if isinstance(obj, (dict, list, tuple, set, frozenset)) or isinstance(obj, BaseModel) or dataclasses.is_dataclass(obj):
        seen.add(id(obj))
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}[{k!r}]", seen)
            yield f"{path}[{k!r}]#key", k
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]", seen)
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield f"{path}.{name}#key", name
            yield from _walk(getattr(obj, name, None), f"{path}.{name}", seen)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            yield f"{path}.{f.name}#key", f.name
            yield from _walk(getattr(obj, f.name, None), f"{path}.{f.name}", seen)


def assert_no_targets(deps: Any, *, agent: str = "agent") -> None:
    """Raise FirewallViolation if `deps` contains a Claim, or any field/key naming a claim target (reported_value...)."""
    for path, v in _walk(deps, "deps", set()):
        if isinstance(v, Claim):
            raise FirewallViolation(f"{agent}: deps contain a Claim at {path}")
        if path.endswith("#key") and isinstance(v, str) and v.lower() in _TARGET_KEYS:
            raise FirewallViolation(f"{agent}: deps expose claim-target field '{v}' at {path[:-4]}")


# ---- loop detection -----------------------------------------------------------------------------------------------

_NOISE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xN"),
    (re.compile(r"(?:[A-Za-z]:)?[\\/][^\s:'\"]+"), "<path>"),
    (re.compile(r"\bline \d+\b", re.I), "line N"),
    (re.compile(r"\d+(?:\.\d+)*"), "N"),
]


def normalize_error(error: str) -> str:
    out = error.lower()
    for rx, repl in _NOISE:
        out = rx.sub(repl, out)
    return re.sub(r"\s+", " ", out).strip()


def loop_key(action_type: str, error: str) -> str:
    """sha256(action_type + normalized_error)"""
    return hashlib.sha256((action_type + "\x00" + normalize_error(error)).encode()).hexdigest()


# ---- guard --------------------------------------------------------------------------------------------------------

class HarnessConfig(BaseModel):
    max_attempts_per_stage: int = 3
    stage_token_budget: int = 200_000
    run_token_budget: int = 1_000_000
    stage_time_budget_s: float = 900.0
    run_time_budget_s: float = 7200.0
    max_validation_retries: int = 1  # invalid structured output: retry once, then escalate


class GuardDecision(BaseModel):
    action: Literal["allow", "retry", "escalate"]
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.action != "escalate"


ALLOW = GuardDecision(action="allow")


class HarnessGuard:
    """One guard per pipeline run. Escalation always means: pause with a `stuck` gate, never fail silently."""

    def __init__(self, config: HarnessConfig | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = config or HarnessConfig()
        self._clock = clock
        self._run_start = clock()
        self._stage_start: dict[str, float] = {}
        self._attempts: dict[str, int] = {}
        self._tokens: dict[str, int] = {}
        self._loops: set[str] = set()
        self._validation_failures: dict[str, int] = {}

    # stages ---------------------------------------------------------------------------------------------------
    def start_stage(self, stage: str) -> None:
        self._stage_start[stage] = self._clock()

    def record_attempt(self, stage: str, action_type: str, error: str | None = None) -> GuardDecision:
        """Call before (re)trying `action_type` in `stage`; pass the error that caused the retry, if any.

        Escalates when the same (action, normalized error) is seen a second time, or attempts exceed the cap.
        """
        if error is not None:
            key = loop_key(action_type, error)
            if key in self._loops:
                return GuardDecision(action="escalate", reason=f"loop: repeated '{action_type}' failure with the same error")
            self._loops.add(key)
        n = self._attempts[stage] = self._attempts.get(stage, 0) + 1
        if n > self.cfg.max_attempts_per_stage:
            return GuardDecision(action="escalate", reason=f"attempt budget exhausted ({self.cfg.max_attempts_per_stage}) in {stage}")
        return ALLOW

    def attempts(self, stage: str) -> int:
        return self._attempts.get(stage, 0)

    # budgets --------------------------------------------------------------------------------------------------
    def charge_tokens(self, stage: str, tokens: int) -> GuardDecision:
        self._tokens[stage] = self._tokens.get(stage, 0) + tokens
        return self.check_budget(stage)

    def check_budget(self, stage: str) -> GuardDecision:
        now = self._clock()
        if self._tokens.get(stage, 0) > self.cfg.stage_token_budget:
            return GuardDecision(action="escalate", reason=f"token budget exceeded in {stage}")
        if sum(self._tokens.values()) > self.cfg.run_token_budget:
            return GuardDecision(action="escalate", reason="run token budget exceeded")
        if stage in self._stage_start and now - self._stage_start[stage] > self.cfg.stage_time_budget_s:
            return GuardDecision(action="escalate", reason=f"time budget exceeded in {stage}")
        if now - self._run_start > self.cfg.run_time_budget_s:
            return GuardDecision(action="escalate", reason="run time budget exceeded")
        return ALLOW

    # structured output ----------------------------------------------------------------------------------------
    def record_validation_failure(self, stage: str) -> GuardDecision:
        n = self._validation_failures[stage] = self._validation_failures.get(stage, 0) + 1
        if n > self.cfg.max_validation_retries:
            return GuardDecision(action="escalate", reason=f"invalid structured output in {stage} after {n - 1} retry")
        return GuardDecision(action="retry", reason="invalid structured output; retrying once")

    # firewall -------------------------------------------------------------------------------------------------
    def check_agent_deps(self, agent: str, deps: Any) -> None:
        """Repair firewall: executor agents never receive claims or reported values."""
        if agent in EXECUTOR_AGENTS:
            assert_no_targets(deps, agent=agent)
