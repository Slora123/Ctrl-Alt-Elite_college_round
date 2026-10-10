import threading
import time

import pytest

from paper_env.domain import (
    Author, CellStatus, DatasetManifest, DatasetSplit, EventType, FailureKind, RunMode, RunStatus, SourceType,
    SplitProvenance,
)


def statuses(st, env):
    return [c.status for c in st.notebook.list_cells(env.id)]


@pytest.fixture
def four(kstack):
    env = kstack.new_ready_env()
    cells = kstack.add_cells(env, "a = 1", "b = a + 1", "c = b + 1", "print(a, b, c)")
    return kstack, env, cells


def test_run_all_success_in_order_with_shared_state(four):
    st, env, cells = four
    run = st.runs.run_all(env.id)
    assert run.status == RunStatus.SUCCESS and run.mode == RunMode.ALL and run.verification_eligible is False
    crs = st.cell_runs(run)
    assert [c.cell_id for c in crs] == [c.id for c in cells]
    assert all(c.status == CellStatus.SUCCESS for c in crs) and crs[3].outputs[0]["text"] == "1 2 3\n"
    assert statuses(st, env) == [CellStatus.SUCCESS] * 4


def test_stop_on_fail_marks_rest_skipped_not_failed(four):
    st, env, cells = four
    st.notebook.apply_edit(cells[1].id, "raise ValueError('broken')", Author.HUMAN)
    run = st.runs.run_all(env.id, stop_on_fail=True)
    assert run.status == RunStatus.FAILED and run.failure_kind == FailureKind.EXPERIMENT
    assert statuses(st, env) == [CellStatus.SUCCESS, CellStatus.FAILED, CellStatus.SKIPPED, CellStatus.SKIPPED]
    crs = st.cell_runs(run)
    assert [c.status for c in crs] == [CellStatus.SUCCESS, CellStatus.FAILED, CellStatus.SKIPPED, CellStatus.SKIPPED]
    assert crs[2].error is None and crs[2].failure_kind is None and "stop_on_fail" in crs[2].outputs[0]["text"]
    assert sum(1 for e in st.captured if e.type == EventType.CELL_FAILED) == 1  # skipped cells never "fail"


def test_continue_on_fail_runs_the_rest(four):
    st, env, cells = four
    st.notebook.apply_edit(cells[1].id, "raise ValueError('broken')", Author.HUMAN)
    run = st.runs.run_all(env.id, stop_on_fail=False)
    assert run.status == RunStatus.FAILED
    got = statuses(st, env)
    assert got[0] == CellStatus.SUCCESS and got[1] == CellStatus.FAILED
    assert got[2] == CellStatus.FAILED  # c = b + 1 -> NameError: b never got defined
    assert st.cell_runs(run)[2].error.type == "NameError" and st.cell_runs(run)[2].failure_kind == FailureKind.EXPERIMENT


def test_run_until_runs_prefix_and_stales_later_cells(four):
    st, env, cells = four
    st.runs.run_all(env.id)
    run = st.runs.run_until(cells[1].id)
    assert run.mode == RunMode.UNTIL and [c.cell_id for c in st.cell_runs(run)] == [cells[0].id, cells[1].id]
    assert statuses(st, env) == [CellStatus.SUCCESS, CellStatus.SUCCESS, CellStatus.STALE, CellStatus.STALE]


def test_run_until_on_fresh_notebook_leaves_never_run_cells_pending(four):
    st, env, cells = four
    st.runs.run_until(cells[1].id)
    assert statuses(st, env) == [CellStatus.SUCCESS, CellStatus.SUCCESS, CellStatus.PENDING, CellStatus.PENDING]


def test_break_then_fix_flow_with_stale_propagation(four):
    st, env, cells = four
    clean = st.runs.run_clean(env.id)
    assert clean.status == RunStatus.SUCCESS
    st.notebook.apply_edit(cells[1].id, "b = a +", Author.AGENT)  # broken edit
    assert statuses(st, env) == [CellStatus.SUCCESS, CellStatus.STALE, CellStatus.STALE, CellStatus.STALE]
    bad = st.runs.run_all(env.id)
    assert statuses(st, env) == [CellStatus.SUCCESS, CellStatus.FAILED, CellStatus.SKIPPED, CellStatus.SKIPPED]
    assert st.cell_runs(bad)[1].error.type == "SyntaxError"
    st.notebook.apply_edit(cells[1].id, "b = a + 1", Author.AGENT)
    assert statuses(st, env) == [CellStatus.SUCCESS, CellStatus.STALE, CellStatus.STALE, CellStatus.STALE]  # skipped cells downstream go stale too
    fixed = st.runs.run_clean(env.id)
    assert fixed.status == RunStatus.SUCCESS and fixed.id not in (clean.id, bad.id)
    assert fixed.notebook_version > clean.notebook_version
    assert len(st.runs.list_runs(env.id)) == 3


def test_run_clean_is_fresh_kernel_fixed_seed_and_only_verification_eligible_mode(kstack):
    env = kstack.new_ready_env(seed=77)
    a, b = kstack.add_cells(env, "import random\nr = random.random()", "print(r)", declared_outputs=["r"])
    live = kstack.runs.run_all(env.id)
    c1 = kstack.runs.run_clean(env.id)
    c2 = kstack.runs.run_clean(env.id)
    assert c1.verification_eligible and c2.verification_eligible
    assert not live.verification_eligible and not kstack.runs.run_until(b.id).verification_eligible
    assert not kstack.runs.run_cell(a.id).verification_eligible
    assert len({live.kernel_session_id, c1.kernel_session_id, c2.kernel_session_id}) == 3
    assert c1.seed == c2.seed == 77 and c1.id != c2.id and c1.notebook_version == c2.notebook_version
    out1, out2 = (kstack.cell_runs(r)[1].outputs[0]["text"] for r in (c1, c2))
    assert out1 == out2  # same seed in a fresh kernel -> same numbers
    assert kstack.cell_runs(c1)[0].state_hash == kstack.cell_runs(c2)[0].state_hash


def test_run_clean_ignores_state_left_in_the_live_kernel(kstack):
    env = kstack.new_ready_env()
    a, b = kstack.add_cells(env, "leftover = 1", "print(leftover)")
    kstack.runs.run_all(env.id)
    kstack.notebook.apply_edit(a.id, "pass", Author.HUMAN)
    live = kstack.runs.run_cell(b.id)  # works only because the old kernel still holds `leftover`
    assert live.status == RunStatus.SUCCESS
    clean = kstack.runs.run_clean(env.id)
    assert clean.status == RunStatus.FAILED and kstack.cell_runs(clean)[1].error.type == "NameError"


def test_incomplete_clean_runs_are_not_verification_eligible(kstack):
    env = kstack.new_ready_env()
    kstack.add_cells(env, "import os\nos._exit(1)")
    run = kstack.runs.run_clean(env.id)
    assert run.status == RunStatus.INFRA_FAILED and run.verification_eligible is False
    env2 = kstack.new_ready_env(alias="p2")
    kstack.add_cells(env2, "raise ValueError('x')")
    failed = kstack.runs.run_clean(env2.id)
    assert failed.status == RunStatus.FAILED and failed.verification_eligible is True  # a real, conclusive failure


def test_infra_failure_mid_run_skips_rest_and_is_distinct(four):
    st, env, cells = four
    st.notebook.apply_edit(cells[1].id, "import os\nos._exit(9)", Author.HUMAN)
    run = st.runs.run_all(env.id, stop_on_fail=False)  # even without stop_on_fail the dead kernel ends the run
    assert run.status == RunStatus.INFRA_FAILED and run.failure_kind == FailureKind.INFRASTRUCTURE
    got = statuses(st, env)
    assert got == [CellStatus.STALE, CellStatus.FAILED, CellStatus.SKIPPED, CellStatus.SKIPPED]
    assert st.notebook.get_cell(cells[1].id).last_failure_kind == FailureKind.INFRASTRUCTURE
    assert "kernel was lost" in st.cell_runs(run)[2].outputs[0]["text"]
    fin = [e for e in st.captured if e.type == EventType.RUN_FINISHED][-1]
    assert fin.payload["failure_kind"] == "infrastructure"


def test_total_run_timeout_stops_run_and_skips_remaining(kstack):
    env = kstack.new_ready_env()
    kstack.add_cells(env, "import time; time.sleep(1.5)", "import time; time.sleep(1.5)", "print('late')", "print('later')")
    run = kstack.runs.run_all(env.id, run_timeout=2.5)
    assert run.status == RunStatus.TIMED_OUT and "total runtime limit" in run.message
    crs = kstack.cell_runs(run)
    assert crs[-1].status == CellStatus.SKIPPED
    assert any(c.status == CellStatus.SKIPPED for c in crs) and "late" not in str([c.outputs for c in crs])


def test_per_cell_timeout_inside_run_all(kstack):
    env = kstack.new_ready_env()
    kstack.add_cells(env, "x = 1", "import time; time.sleep(30)", "print('after')")
    run = kstack.runs.run_all(env.id, cell_timeout=1.0)
    crs = kstack.cell_runs(run)
    assert crs[1].error.type == "CellTimeout" and crs[1].failure_kind == FailureKind.EXPERIMENT
    assert crs[2].status == CellStatus.SKIPPED and run.status == RunStatus.FAILED


def test_interrupt_run_cancels_and_skips_rest(kstack):
    env = kstack.new_ready_env()
    kstack.add_cells(env, "x = 1", "import time; time.sleep(60)", "print('never')")
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("run", kstack.runs.run_all(env.id)))
    th.start()
    end = time.time() + 30
    while sum(e.type == EventType.CELL_STARTED for e in kstack.captured) < 2 and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.5)
    run_id = kstack.runs.list_runs(env.id)[0].id
    assert kstack.runs.interrupt(run_id)
    th.join(30)
    run = out["run"]
    assert run.status == RunStatus.CANCELLED and run.failure_kind is None
    assert [c.status for c in kstack.cell_runs(run)] == [CellStatus.SUCCESS, CellStatus.FAILED, CellStatus.SKIPPED]
    assert kstack.cell_runs(run)[1].error.type == "Interrupted"


def test_run_records_approximate_split_and_dataset_manifests(kstack, tmp_path):
    d = tmp_path / "ds"; d.mkdir(); (d / "f.txt").write_text("1")
    kstack.datasets.register(DatasetManifest(
        dataset_id="ap@v1", name="ap", version="v1", source_type=SourceType.LOCAL_PATH, source_uri=str(d),
        splits=[DatasetSplit(name="test", provenance=SplitProvenance.APPROXIMATE)]))
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, ["ap@v1"])
    kstack.add_cells(env, "x = 1")
    run = kstack.runs.run_clean(env.id)
    assert run.approximate_split is True and run.dataset_manifests[0]["dataset_id"] == "ap@v1"
    assert run.hardware_info["lockfile_hash"] == run.env_lockfile_hash
