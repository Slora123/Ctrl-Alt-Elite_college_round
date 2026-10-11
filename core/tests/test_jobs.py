from datetime import datetime, timedelta, timezone

import pytest

from crisisgrid_core.jobs import TERMINAL, Capabilities, JobRequirements, can_transition, failure_outcome, is_stale, match


@pytest.mark.parametrize("a,b,ok", [
    ("queued", "assigned", True), ("assigned", "running", True), ("running", "succeeded", True), ("running", "failed", True),
    ("running", "timeout", True), ("running", "infra_failed", True), ("assigned", "queued", True), ("queued", "cancelled", True),
    ("queued", "running", False), ("queued", "succeeded", False), ("succeeded", "running", False), ("failed", "queued", False),
    ("infra_failed", "queued", False), ("cancelled", "queued", False), ("running", "queued", False), ("running", "assigned", False),
])
def test_transitions(a, b, ok):
    assert can_transition(a, b) is ok


def test_terminal_states_have_no_exits():
    for t in TERMINAL:
        assert not any(can_transition(t, s) for s in ("queued", "assigned", "running", *TERMINAL))


@pytest.mark.parametrize("req,caps,ok,fragment", [
    ({}, {}, True, None),
    ({"gpu_required": True}, {"gpu": False}, False, "GPU"),
    ({"gpu_required": True}, {"gpu": True}, True, None),
    ({"needs_network": True}, {"network": False}, False, "network"),
    ({"est_runtime_s": 7200}, {"max_runtime_s": 3600}, False, "exceeds"),
    ({"python": "3.10"}, {"python_versions": ["3.12"]}, False, "python 3.10"),
    ({"require_strong_isolation": True}, {"isolation": "subprocess"}, False, "isolation"),
    ({"require_strong_isolation": True}, {"isolation": "docker"}, True, None),
    ({"gpu_required": True, "needs_network": True}, {"gpu": False, "network": False}, False, "GPU"),
])
def test_capability_matching(req, caps, ok, fragment):
    got, why = match(JobRequirements(**req), Capabilities(**caps))
    assert got is ok and (fragment is None or any(fragment in w for w in why))


def test_matching_lists_every_reason():
    _, why = match(JobRequirements(gpu_required=True, needs_network=True), Capabilities(gpu=False, network=False))
    assert len(why) == 2


@pytest.mark.parametrize("status,attempt,maxr,expected", [
    ("infra_failed", 0, 1, "requeue"), ("infra_failed", 1, 1, "final"), ("infra_failed", 0, 0, "final"),
    ("failed", 0, 5, "final"),       # user-code failure: never retried
    ("timeout", 0, 5, "final"),
    ("succeeded", 0, 5, "final"), ("cancelled", 0, 5, "final"),
])
def test_retry_only_on_infrastructure_failure(status, attempt, maxr, expected):
    assert failure_outcome(status, attempt, maxr) == expected


def test_staleness():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert not is_stale(t, t + timedelta(seconds=30), timedelta(seconds=60))
    assert is_stale(t, t + timedelta(seconds=61), timedelta(seconds=60))


def test_staleness_tolerates_naive_datetimes_from_sqlite():
    naive = datetime(2026, 1, 1)
    aware = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    assert is_stale(naive, aware, timedelta(seconds=60))


def test_a_job_without_a_python_requirement_runs_on_any_worker_interpreter():
    # regression: jobs used to demand 3.12, so a worker on 3.13 (or 3.11) could never run anything
    for version in ("3.11", "3.12", "3.13"):
        assert match(JobRequirements(), Capabilities(python_versions=[version]))[0]
    assert not match(JobRequirements(python="3.12"), Capabilities(python_versions=["3.13"]))[0]
