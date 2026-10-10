import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from paper_env.backends import LocalBackend
from paper_env.domain import (
    Author, CellStatus, DecisionKind, EventType, FailureKind, RunMode, RunStatus,
)
from paper_env.hooks import Decision
from paper_env.services.runs import EnvNotReady


def types(st):
    return st.event_types()


def one(st, code, **kw):
    env = st.new_ready_env()
    (cell,) = st.add_cells(env, code, **kw)
    run = st.runs.run_cell(cell.id)
    return env, cell, run, st.cell_runs(run)[0]


# ------------------------------------------------------------------ success path
def test_success_streams_output_and_records_everything(kstack):
    env, cell, run, cr = one(kstack, "print('hello')\nimport sys; print('oops', file=sys.stderr)\n1 + 1")
    assert run.status == RunStatus.SUCCESS and run.mode == RunMode.CELL and run.finished_at
    assert cr.status == CellStatus.SUCCESS and cr.failure_kind is None and cr.duration > 0
    streams = {o["name"]: o["text"] for o in cr.outputs if o["type"] == "stream"}
    assert streams == {"stdout": "hello\n", "stderr": "oops\n"}
    assert any(o["type"] == "execute_result" and o["data"]["text/plain"] == "2" for o in cr.outputs)
    assert kstack.cell(cell.id).status == CellStatus.SUCCESS
    t = types(kstack)
    assert t.index(EventType.RUN_STARTED) < t.index(EventType.CELL_STARTED) < t.index(EventType.CELL_OUTPUT) \
        < t.index(EventType.CELL_FINISHED) < t.index(EventType.RUN_FINISHED)
    out_events = [e for e in kstack.captured if e.type == EventType.CELL_OUTPUT]
    assert {e.payload["name"] for e in out_events if e.payload["type"] == "stream"} == {"stdout", "stderr"}


def test_run_record_captures_everything_that_affects_results(kstack):
    env, cell, run, cr = one(kstack, "x = 1")
    assert run.env_lockfile_hash == kstack.template["lockfile_hash"] and run.seed == 0
    assert run.notebook_version == 1 and run.backend == "local-cpu" and run.verification_eligible is False
    hw = run.hardware_info
    assert hw["env_python"]["python_version"].startswith("3.12") and hw["platform"] and "cuda" in hw
    assert hw["seeded"]["python"] is True and hw["seeded"]["numpy"] == "not_installed"
    assert run.kernel_session_id


def test_same_version_run_twice_gives_two_runs(kstack):
    env, cell, r1, _ = one(kstack, "x = 1")
    r2 = kstack.runs.run_cell(cell.id)
    assert r1.id != r2.id and r1.notebook_version == r2.notebook_version
    assert [r.id for r in kstack.runs.list_runs(env.id)] == [r1.id, r2.id]
    assert r1.kernel_session_id == r2.kernel_session_id  # same live kernel


def test_state_persists_between_run_cell_calls_and_downstream_goes_stale(kstack):
    env = kstack.new_ready_env()
    a, b = kstack.add_cells(env, "x = 41", "print(x + 1)")
    kstack.runs.run_cell(a.id)
    r = kstack.runs.run_cell(b.id)
    assert kstack.cell_runs(r)[0].outputs[0]["text"] == "42\n"
    assert kstack.cell(b.id).status == CellStatus.SUCCESS
    kstack.runs.run_cell(a.id)  # re-running a makes b stale
    assert kstack.cell(a.id).status == CellStatus.SUCCESS and kstack.cell(b.id).status == CellStatus.STALE


# ------------------------------------------------------------------ experiment failure
def test_cell_exception_is_an_experiment_failure_with_failing_line(kstack):
    src = "a = 1\nb = 2\nraise ValueError('bad thing')\nc = 3"
    env, cell, run, cr = one(kstack, src)
    assert run.status == RunStatus.FAILED and run.failure_kind == FailureKind.EXPERIMENT
    assert cr.status == CellStatus.FAILED and cr.failure_kind == FailureKind.EXPERIMENT
    assert (cr.error.type, cr.error.message) == ("ValueError", "bad thing")
    assert cr.error.failing_lineno == 3 and cr.error.failing_line == "raise ValueError('bad thing')"
    assert "ValueError" in cr.error.traceback and "\x1b" not in cr.error.traceback
    c = kstack.cell(cell.id)
    assert c.status == CellStatus.FAILED and c.last_failure_kind == FailureKind.EXPERIMENT
    ev = [e for e in kstack.captured if e.type == EventType.CELL_FAILED][0]
    assert ev.payload["exception_type"] == "ValueError" and ev.payload["failing_lineno"] == 3
    assert ev.payload["cell_version_id"] == cr.cell_version_id and ev.payload["failure_kind"] == "experiment"
    assert EventType.KERNEL_DIED not in types(kstack)


def test_kernel_survives_an_exception_and_keeps_state(kstack):
    env = kstack.new_ready_env()
    a, b, c = kstack.add_cells(env, "keep = 'still here'", "1/0", "print(keep)")
    for x in (a, b, c):
        kstack.runs.run_cell(x.id)
    assert kstack.cell(b.id).status == CellStatus.FAILED
    assert kstack.cell_runs(kstack.runs.list_runs(env.id)[-1])[0].outputs[0]["text"] == "still here\n"


def test_syntax_error_reported_as_experiment_failure(kstack):
    env, cell, run, cr = one(kstack, "x = (")
    assert cr.failure_kind == FailureKind.EXPERIMENT and cr.error.type == "SyntaxError"


# ------------------------------------------------------------------ timeout / interrupt
def test_cell_timeout_interrupts_and_kernel_survives(kstack_factory):
    st = kstack_factory()
    env = st.new_ready_env()
    a, b = st.add_cells(env, "import time\nkeep = 7\ntime.sleep(60)", "print(keep)")
    t0 = time.monotonic()
    run = st.runs.run_cell(a.id, timeout=1.5)
    assert time.monotonic() - t0 < 20
    cr = st.cell_runs(run)[0]
    assert cr.status == CellStatus.FAILED and cr.error.type == "CellTimeout" and cr.failure_kind == FailureKind.EXPERIMENT
    assert run.status == RunStatus.FAILED
    r2 = st.runs.run_cell(b.id)
    assert st.cell_runs(r2)[0].outputs[0]["text"] == "7\n"  # same kernel, state before the sleep intact


def test_unresponsive_cell_after_timeout_kills_kernel_as_infra_failure(kstack_factory):
    st = kstack_factory(interrupt_grace_s=1.0)
    env = st.new_ready_env()
    (a,) = st.add_cells(env, "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)")
    run = st.runs.run_cell(a.id, timeout=1.0)
    cr = st.cell_runs(run)[0]
    assert cr.failure_kind == FailureKind.INFRASTRUCTURE and cr.error.type == "killed_after_timeout"
    assert run.status == RunStatus.INFRA_FAILED and run.failure_kind == FailureKind.INFRASTRUCTURE
    assert st.runner.get_session(env.id).alive is False
    assert EventType.KERNEL_DIED in types(st)


def test_user_interrupt_marks_cell_interrupted_not_failed_experiment(kstack):
    env = kstack.new_ready_env()
    a, b = kstack.add_cells(env, "import time\ntime.sleep(60)", "print('after')")
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("run", kstack.runs.run_cell(a.id)))
    th.start()
    deadline = time.time() + 30
    while EventType.CELL_STARTED not in types(kstack) and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert kstack.runner.interrupt(env.id) is True
    th.join(30)
    run = out["run"]
    cr = kstack.cell_runs(run)[0]
    assert cr.error.type == "Interrupted" and cr.failure_kind is None
    assert run.status == RunStatus.CANCELLED
    r2 = kstack.runs.run_cell(b.id)  # kernel still usable
    assert r2.status == RunStatus.SUCCESS


# ------------------------------------------------------------------ kernel death / OOM
def test_kernel_exit_is_infrastructure_failure_and_state_is_not_pretended(kstack):
    env = kstack.new_ready_env()
    a, b, c = kstack.add_cells(env, "x = 1", "import os\nos._exit(3)", "print(x)")
    kstack.runs.run_cell(a.id)
    run = kstack.runs.run_cell(b.id)
    cr = kstack.cell_runs(run)[0]
    assert cr.failure_kind == FailureKind.INFRASTRUCTURE and cr.error.type == "kernel_died" and "3" in cr.error.message
    assert run.status == RunStatus.INFRA_FAILED and run.failure_kind == FailureKind.INFRASTRUCTURE
    ev = [e for e in kstack.captured if e.type == EventType.KERNEL_DIED][0]
    assert ev.payload["state_lost"] is True and ev.payload["failure_kind"] == "infrastructure" and ev.payload["exit_code"] == 3
    assert kstack.cell(b.id).last_failure_kind == FailureKind.INFRASTRUCTURE
    assert kstack.cell(a.id).status == CellStatus.STALE  # its in-memory result died with the kernel
    assert EventType.CELL_FAILED not in types(kstack)  # not reported as an experiment failure
    # running on the dead session must not pretend state survived
    r3 = kstack.runs.run_cell(c.id)  # run_cell starts a fresh kernel; x is gone -> experiment NameError
    assert kstack.cell_runs(r3)[0].error.type == "NameError"


def test_sigkill_is_classified_as_possible_oom(kstack):
    env, cell, run, cr = one(kstack, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)")
    assert cr.failure_kind == FailureKind.INFRASTRUCTURE and cr.error.type == "oom_or_killed"
    assert "possibly out of memory" in cr.error.message
    assert [e for e in kstack.captured if e.type == EventType.KERNEL_DIED][0].payload["exit_code"] == -9


def test_segfault_classified(kstack):
    env, cell, run, cr = one(kstack, "import ctypes\nctypes.string_at(0)")
    assert cr.failure_kind == FailureKind.INFRASTRUCTURE and cr.error.type == "segfault"


def test_python_memoryerror_is_an_experiment_failure_not_infra(kstack):
    env, cell, run, cr = one(kstack, "raise MemoryError('simulated')")
    assert cr.failure_kind == FailureKind.EXPERIMENT and cr.error.type == "MemoryError"


# ------------------------------------------------------------------ restart / shutdown
def test_restart_kernel_loses_state_and_marks_all_stale(kstack):
    env = kstack.new_ready_env()
    a, b = kstack.add_cells(env, "v = 5", "print(v)")
    kstack.runs.run_cell(a.id); kstack.runs.run_cell(b.id)
    old = kstack.runner.get_session(env.id)
    new = kstack.runner.restart_kernel(env.id)
    assert new.id != old.id and new.seed == old.seed
    assert kstack.cell(a.id).status == CellStatus.STALE and kstack.cell(b.id).status == CellStatus.STALE
    r = kstack.runs.run_cell(b.id)
    assert kstack.cell_runs(r)[0].error.type == "NameError"
    assert any(e.payload.get("op") == "kernel_restart" for e in kstack.captured if e.type == EventType.ENV_CHANGED)


def test_clean_shutdown_kills_kernel_process(kstack):
    env, cell, run, _ = one(kstack, "x = 1")
    pid = kstack.runner.get_session(env.id).handle.info["pid"]
    assert _alive(pid)
    kstack.runner.shutdown()
    _wait_dead(pid)
    assert not (kstack.settings.kernels_dir() / "x").exists()
    assert list(kstack.settings.kernels_dir().glob("*.json")) == []


def test_orphaned_kernels_are_cleaned_up_on_startup(kstack):
    env, cell, run, _ = one(kstack, "x = 1")
    h = kstack.runner.get_session(env.id).handle
    pid = h.info["pid"]
    reg = kstack.settings.kernels_dir() / f"{h.id}.json"
    info = json.loads(reg.read_text())
    info["owner_pid"] = _dead_pid()  # simulate: the server that owned it crashed
    reg.write_text(json.dumps(info))
    fresh = LocalBackend(kstack.settings, kstack.scrubber)  # "new server process" with no live handles
    assert fresh.cleanup_orphans() == [h.id]
    _wait_dead(pid)
    assert not reg.exists()


def test_cleanup_does_not_touch_live_servers_kernels(kstack):
    env, cell, run, _ = one(kstack, "x = 1")
    h = kstack.runner.get_session(env.id).handle
    assert kstack.backend.cleanup_orphans() == []
    assert _alive(h.info["pid"])


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_dead(pid, t=15):
    end = time.time() + t
    while time.time() < end:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not _alive(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"pid {pid} still alive")


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


# ------------------------------------------------------------------ outputs
def test_huge_output_truncated_in_db_but_full_in_artifact_file(kstack_factory):
    st = kstack_factory(max_output_bytes_db=2000)
    env, cell, run, cr = one(st, "for i in range(3000):\n    print('line', i)")
    assert cr.status == CellStatus.SUCCESS and cr.output_truncated
    db_bytes = sum(len(o.get("text", "")) for o in cr.outputs)
    assert db_bytes <= 2200 and cr.outputs[-1]["type"] == "truncated"
    full = Path(cr.full_output_path).read_text()
    assert "line 2999" in full and full.count("line") >= 3000
    assert cr.full_output_path.startswith(str(st.settings.artifacts_dir(env.id)))


def test_output_cap_interrupts_runaway_cell(kstack_factory):
    st = kstack_factory(max_output_bytes_db=1000, max_output_bytes_total=50_000)
    env, cell, run, cr = one(st, "i = 0\nwhile True:\n    print('x' * 100)\n    i += 1")
    assert cr.error.type == "OutputLimitExceeded" and cr.failure_kind == FailureKind.EXPERIMENT
    assert cr.output_truncated and run.status == RunStatus.FAILED
    # kernel was interrupted, not killed
    assert st.runner.get_session(env.id).alive


def test_small_output_has_no_artifact_file(kstack):
    env, cell, run, cr = one(kstack, "print('tiny')")
    assert cr.full_output_path is None and not cr.output_truncated


# ------------------------------------------------------------------ secrets
def test_secrets_never_reach_outputs_events_or_artifacts(kstack_factory):
    st = kstack_factory(max_output_bytes_db=300)
    st.scrubber.add_secrets(["s3cr3t-registered-value"])
    token = "hf_" + "AbC1" * 8
    code = (
        "import sys\n"
        "print('registered: s3cr3t-registered-value')\n"
        f"print('pattern: {token}')\n"
        "print('split-', end=''); print('unused')\n"
        "print('s3cr3t-regis', end=''); print('tered-value done')\n"
        "print('API_KEY=abcdef123456789', file=sys.stderr)\n"
        "print('padding ' * 400)\n"
        "display({'text/plain': 'disp s3cr3t-registered-value'}, raw=True)\n"
        "raise RuntimeError('boom s3cr3t-registered-value')\n"
    )
    env, cell, run, cr = one(st, code)
    blobs = [json.dumps(cr.outputs), json.dumps(cr.error.model_dump()), Path(cr.full_output_path).read_text(),
             json.dumps([e.model_dump(mode="json") for e in st.bus.history(limit=100000)])]
    for blob in blobs:
        assert "s3cr3t-registered-value" not in blob and token not in blob and "abcdef123456789" not in blob
    assert "[REDACTED]" in blobs[0]


def test_env_secrets_do_not_leak_into_the_kernel(kstack, monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "super-secret-env-value")
    st = kstack
    env, cell, run, cr = one(st, "import os\nprint(sorted(k for k in os.environ if 'TOKEN' in k or 'SECRET' in k))")
    assert cr.outputs[0]["text"] == "[]\n"


# ------------------------------------------------------------------ seed / state hash
def test_seed_makes_python_randomness_reproducible_across_kernels(kstack):
    env = kstack.new_ready_env()
    (a,) = kstack.add_cells(env, "import random\nprint(random.random(), hash('abc'))")
    outs = []
    for seed in (123, 123, 999):
        kstack.runner.shutdown()
        run = kstack.runs.run_cell(a.id, seed=seed)
        outs.append(kstack.cell_runs(run)[0].outputs[0]["text"])
    assert outs[0] == outs[1] and outs[0] != outs[2]


def test_state_hash_depends_on_declared_output_values(kstack):
    env = kstack.new_ready_env()
    (a,) = kstack.add_cells(env, "x = [1, 2, 3]\ny = 'z'", declared_outputs=["x"])
    h1 = kstack.cell_runs(kstack.runs.run_cell(a.id))[0].state_hash
    kstack.runner.shutdown()
    h2 = kstack.cell_runs(kstack.runs.run_cell(a.id))[0].state_hash
    assert h1 and h1 == h2  # deterministic across kernels
    kstack.notebook.apply_edit(a.id, "x = [1, 2, 4]\ny = 'z'", Author.HUMAN)
    h3 = kstack.cell_runs(kstack.runs.run_cell(a.id))[0].state_hash
    kstack.notebook.apply_edit(a.id, "x = [1, 2, 3]\ny = 'changed but undeclared'", Author.HUMAN)
    h4 = kstack.cell_runs(kstack.runs.run_cell(a.id))[0].state_hash
    assert h3 != h1 and h4 == h1  # only declared outputs matter


def test_state_hash_without_declared_outputs_uses_namespace_summary(kstack):
    env, cell, run, cr = one(kstack, "alpha = 1\nbeta = [2]")
    assert cr.state_hash and len(cr.state_hash) == 64


def test_failed_cell_has_no_state_hash(kstack):
    env, cell, run, cr = one(kstack, "1/0")
    assert cr.state_hash is None


# ------------------------------------------------------------------ hook
def test_hook_deny_blocks_execution_cleanly_then_resume_allows(kstack_factory):
    class Deny:
        def check(self, a): return Decision.deny("not today")

    st = kstack_factory(hook=Deny())
    env = st.new_ready_env()
    a, b = st.add_cells(env, "print('ran a')", "print('ran b')")
    run = st.runs.run_cell(a.id)
    assert run.status == RunStatus.BLOCKED and run.finished_at is None
    cr = st.cell_runs(run)[0]
    assert cr.status == CellStatus.BLOCKED and cr.blocked_action_id and cr.outputs == []
    assert st.cell(a.id).status == CellStatus.BLOCKED
    assert EventType.CELL_STARTED not in types(st) and EventType.ACTION_BLOCKED in types(st)
    out = st.gate.resume_blocked(cr.blocked_action_id, Decision.allow("approved"))
    assert out.status == RunStatus.SUCCESS
    cr2 = st.cell_runs(run)[0]
    assert cr2.status == CellStatus.SUCCESS and cr2.outputs[0]["text"] == "ran a\n" and cr2.blocked_action_id is None
    assert st.cell(a.id).status == CellStatus.SUCCESS


def test_hook_deny_final_skips_cell_and_cancels_run(kstack_factory):
    class Ask:
        def check(self, a): return Decision.needs_approval("review please")

    st = kstack_factory(hook=Ask())
    env = st.new_ready_env()
    (a,) = st.add_cells(env, "print('never')")
    run = st.runs.run_cell(a.id)
    cr = st.cell_runs(run)[0]
    assert st.gate.resume_blocked(cr.blocked_action_id, Decision.deny("rejected")) is None
    run = st.runs.get_run(run.id)
    assert run.status == RunStatus.CANCELLED and "denied" in run.message
    assert st.cell_runs(run)[0].status == CellStatus.SKIPPED and st.cell(a.id).status == CellStatus.SKIPPED
    assert EventType.CELL_STARTED not in types(st)


# ------------------------------------------------------------------ preconditions
def test_run_requires_a_built_env(kstack):
    env = kstack.new_paper_env(alias="nolock")
    (c,) = kstack.add_cells(env, "x = 1")
    with pytest.raises(EnvNotReady):
        kstack.runs.run_cell(c.id)


def test_missing_interpreter_is_infra_failure_with_clear_message(kstack):
    env = kstack.new_ready_env()
    import shutil
    shutil.rmtree(kstack.settings.venv_dir(env.id))
    (c,) = kstack.add_cells(env, "x = 1")
    run = kstack.runs.run_cell(c.id)
    assert run.status == RunStatus.INFRA_FAILED and "could not start kernel" in run.message
    assert kstack.cell_runs(run) == []  # nothing executed


def test_runner_only_uses_backend_interface(kstack):
    """The runner/run services must never reach into LocalBackend internals."""
    import inspect, paper_env.kernel.runner as r, paper_env.services.runs as s
    for mod in (r, s):
        src = inspect.getsource(mod)
        assert "jupyter_client" not in src and "LocalBackend" not in src
