from dataclasses import dataclass, field

import pytest
from pydantic import BaseModel

from crisisgrid_core.harness import (
    FirewallViolation,
    HarnessConfig,
    HarnessGuard,
    assert_no_targets,
    loop_key,
    normalize_error,
)
from crisisgrid_core.models import Claim, Source


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def claim():
    return Claim(text="t", metric="top1", dataset="d", split="test", reported_value=91.2, unit="percent", tolerance=0.5,
                 source=Source(kind="paper", ref="p"), provenance="stated")


# ---- loop detection / attempts ------------------------------------------------------------------------------------

def test_same_error_twice_escalates_on_second_attempt():
    g = HarnessGuard()
    err = "ModuleNotFoundError: No module named 'torchvision' (line 12)"
    assert g.record_attempt("execute", "run_cell", err).action == "allow"
    d = g.record_attempt("execute", "run_cell", err.replace("12", "57"))  # line number differs, same error
    assert d.action == "escalate" and "loop" in d.reason


def test_different_errors_do_not_trip_the_loop_detector():
    g = HarnessGuard()
    assert g.record_attempt("execute", "run_cell", "ImportError: a").ok
    assert g.record_attempt("execute", "run_cell", "KeyError: b").ok


def test_same_error_different_action_is_not_a_loop():
    g = HarnessGuard()
    assert g.record_attempt("execute", "run_cell", "boom").ok
    assert g.record_attempt("execute", "install_package", "boom").ok


def test_attempt_cap():
    g = HarnessGuard(HarnessConfig(max_attempts_per_stage=3))
    assert [g.record_attempt("env", "build", f"err {chr(97 + i)}").action for i in range(4)] == ["allow"] * 3 + ["escalate"]
    assert g.record_attempt("other_stage", "build").ok  # attempts are per stage


@pytest.mark.parametrize("a,b", [
    ("Error at 0xDEADBEEF", "error at 0x1234"),
    ("FileNotFoundError: /tmp/a/b.csv", "FileNotFoundError: C:\\Users\\x\\y.csv"),
    ("CUDA out of memory. Tried to allocate 2.00 GiB", "cuda out of memory. tried to allocate 3.50 gib"),
])
def test_normalize_error_collapses_noise(a, b):
    assert normalize_error(a) == normalize_error(b)
    assert loop_key("x", a) == loop_key("x", b)


def test_normalize_error_keeps_different_errors_apart():
    assert loop_key("x", "ImportError: foo") != loop_key("x", "ImportError: bar")


# ---- budgets ------------------------------------------------------------------------------------------------------

def test_stage_and_run_token_budgets():
    g = HarnessGuard(HarnessConfig(stage_token_budget=100, run_token_budget=150))
    assert g.charge_tokens("a", 100).ok
    assert g.charge_tokens("a", 1).action == "escalate"
    g2 = HarnessGuard(HarnessConfig(stage_token_budget=100, run_token_budget=150))
    assert g2.charge_tokens("a", 90).ok and g2.charge_tokens("b", 90).action == "escalate"


def test_time_budgets_use_injected_clock():
    clk = Clock()
    g = HarnessGuard(HarnessConfig(stage_time_budget_s=10, run_time_budget_s=100), clock=clk)
    g.start_stage("s")
    clk.t = 9
    assert g.check_budget("s").ok
    clk.t = 11
    d = g.check_budget("s")
    assert d.action == "escalate" and "time" in d.reason
    clk.t = 101
    assert "run time" in g.check_budget("fresh").reason


# ---- structured output --------------------------------------------------------------------------------------------

def test_validation_failure_retries_once_then_escalates():
    g = HarnessGuard()
    assert g.record_validation_failure("parse").action == "retry"
    assert g.record_validation_failure("parse").action == "escalate"
    assert g.record_validation_failure("audit").action == "retry"  # per stage


# ---- repair firewall ----------------------------------------------------------------------------------------------

@dataclass
class Deps:
    name: str = "x"
    extra: dict = field(default_factory=dict)


class PModel(BaseModel):
    note: str = ""
    payload: object = None


@pytest.mark.parametrize("deps", [
    Deps(extra={"c": claim()}),
    Deps(extra={"nested": [{"deep": (claim(),)}]}),
    {"reported_value": 91.2},
    Deps(extra={"x": {"Reported_Value": 1}}),
    PModel(payload=[claim()]),
    PModel(payload={"baseline_value": 3}),
    [Deps(extra={"claims": []})],
])
def test_firewall_rejects_targets(deps):
    with pytest.raises(FirewallViolation):
        assert_no_targets(deps)


@pytest.mark.parametrize("deps", [
    Deps(extra={"error": "ImportError", "traceback": "...", "env": {"python": "3.12"}}),
    {"cells": ["x = 1"], "metric_name": "top1", "seed": 0},
    PModel(note="reported nothing", payload=[1, 2, {"a": "b"}]),
    None, 5, "plain string reported_value",  # values are not keys
])
def test_firewall_allows_clean_deps(deps):
    assert_no_targets(deps)


def test_firewall_handles_cycles():
    a: dict = {}
    a["self"] = a
    assert_no_targets(a)


@pytest.mark.parametrize("agent", ["code_rebuilder", "run_diagnoser", "env_builder"])
def test_guard_blocks_claims_for_executor_agents(agent):
    with pytest.raises(FirewallViolation):
        HarnessGuard().check_agent_deps(agent, Deps(extra={"c": claim()}))


@pytest.mark.parametrize("agent", ["claim_verifier", "report_writer"])
def test_guard_allows_claims_for_verifier_and_reporter(agent):
    HarnessGuard().check_agent_deps(agent, Deps(extra={"c": claim()}))


def test_reset_validation_gives_a_fresh_retry():
    g = HarnessGuard()
    assert g.record_validation_failure("parse").action == "retry"
    g.reset_validation("parse")
    assert g.record_validation_failure("parse").action == "retry"
