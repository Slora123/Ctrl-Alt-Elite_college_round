import json
from pathlib import Path

import pytest

from paper_env.domain import Author, CellStatus, EventType, RunMode, RunStatus
from paper_env.services.checkpoints import CheckpointError


def setup_rng_notebook(st, alias="ck"):
    env = st.new_ready_env(alias=alias, seed=5)
    cells = st.add_cells(env, "import random\nx = [random.random() for _ in range(3)]", declared_outputs=["x"])
    cells += st.add_cells(env, "y = random.random()", declared_inputs=["random"], declared_outputs=["y"])
    cells += st.add_cells(env, "z = (x, y, random.random())\nprint(z)", declared_inputs=["x", "y", "random"], declared_outputs=["z"])
    return env, cells


def test_checkpoint_saved_after_each_cell_when_enabled(kstack):
    env, cells = setup_rng_notebook(kstack)
    run = kstack.runs.run_all(env.id, checkpoint=True)
    cps = kstack.checkpoints.list(env.id, run.id)
    assert [c.cell_id for c in cps] == [c.id for c in cells]
    assert cps[0].saved == ["x"] and cps[2].saved == ["x", "y", "z"]  # cumulative declared outputs
    assert all(c.rng_saved and c.unserializable == [] for c in cps)
    assert Path(cps[1].path, "manifest.json").is_file()
    assert [e.payload["ok"] for e in kstack.captured if e.type == EventType.CHECKPOINT_SAVED] == [True] * 3


def test_checkpointing_is_off_by_default_and_configurable(kstack_factory):
    st = kstack_factory()
    env, _ = setup_rng_notebook(st)
    st.runs.run_all(env.id)
    assert st.checkpoints.list(env.id) == []
    st2 = kstack_factory(checkpoint_after_each_cell=True)
    env2, _ = setup_rng_notebook(st2, "ck2")
    st2.runs.run_all(env2.id)
    assert len(st2.checkpoints.list(env2.id)) == 3


def test_resume_restores_variables_and_rng_so_results_match_uninterrupted_run(kstack):
    env, cells = setup_rng_notebook(kstack)
    full = kstack.runs.run_all(env.id, checkpoint=True)
    full_crs = kstack.cell_runs(full)
    expected_out, expected_hash = full_crs[2].outputs[0]["text"], full_crs[2].state_hash
    cp_after_cell_2 = kstack.checkpoints.list(env.id, full.id)[1]
    kstack.runner.shutdown()  # the process "crashes": all in-memory state gone

    res, cont = kstack.runs.continue_from(cp_after_cell_2.id)
    assert res.complete and res.restored == ["x", "y"] and set(res.rng_restored) == {"python"} and res.stale_cells == [cells[2].id]
    assert cont.status == RunStatus.SUCCESS and cont.mode == RunMode.ALL and cont.verification_eligible is False
    cr = kstack.cell_runs(cont)
    assert [c.cell_id for c in cr] == [cells[2].id]  # only the remaining cell ran
    assert cr[0].outputs[0]["text"] == expected_out  # includes a fresh random draw: RNG was restored
    assert cr[0].state_hash == expected_hash
    assert EventType.CHECKPOINT_RESTORED in kstack.event_types()


def test_resume_without_rng_restore_would_differ(kstack):
    """Control: proves the previous test is sensitive to RNG restoration (a re-seeded kernel diverges)."""
    env, cells = setup_rng_notebook(kstack, "ctl")
    full = kstack.runs.run_all(env.id, checkpoint=True)
    expected = kstack.cell_runs(full)[2].outputs[0]["text"]
    kstack.runner.shutdown()
    kstack.runs.run_all(env.id)  # fresh state re-seeded with seed 5, not the checkpointed stream position
    again = kstack.cell_runs(kstack.runs.list_runs(env.id)[-1])[2].outputs[0]["text"]
    assert again == expected  # same seed + same order => same; baseline for determinism
    kstack.runner.shutdown()
    kstack.notebook.apply_edit(cells[0].id, "import random\nx = [random.random() for _ in range(3)]\nrandom.random()", Author.HUMAN)
    kstack.runs.run_all(env.id)
    changed = kstack.cell_runs(kstack.runs.list_runs(env.id)[-1])[2].outputs[0]["text"]
    assert changed != expected


def test_unserializable_outputs_are_recorded_and_downstream_goes_stale_explicitly(kstack):
    env = kstack.new_ready_env()
    cells = kstack.add_cells(env, "import socket\nsock = socket.socket()\ngen = (i for i in range(3))\nnum = 4",
                             declared_outputs=["sock", "gen", "num"])
    cells += kstack.add_cells(env, "num2 = num * 2", declared_inputs=["num"], declared_outputs=["num2"])
    run = kstack.runs.run_all(env.id, checkpoint=True)
    cp = kstack.checkpoints.list(env.id, run.id)[0]
    assert cp.saved == ["num"]
    assert {x.name for x in cp.unserializable} == {"sock", "gen"}
    assert all(x.reason for x in cp.unserializable)
    ev = [e for e in kstack.captured if e.type == EventType.CHECKPOINT_SAVED][0]
    assert {x["name"] for x in ev.payload["unserializable"]} == {"sock", "gen"}  # reported, not silent
    kstack.runner.shutdown()
    res = kstack.checkpoints.resume_from(cp.id)
    assert not res.complete and {x["name"] for x in res.not_restored} == {"sock", "gen"}
    assert res.restored == ["num"]
    assert any("could not be restored" in m and "sock" in m for m in res.messages)
    # cell 0's outputs are partly gone: it and everything after are stale; nothing pretends otherwise
    assert [kstack.cell(c.id).status for c in cells] == [CellStatus.STALE, CellStatus.STALE]
    assert res.stale_cells == [c.id for c in cells]
    ev = [e for e in kstack.captured if e.type == EventType.CHECKPOINT_RESTORED][0]
    assert ev.payload["complete"] is False and ev.payload["stale_cells"] == res.stale_cells


def test_resume_keeps_restored_prefix_success_and_marks_later_cells_stale(kstack):
    env, cells = setup_rng_notebook(kstack, "pref")
    run = kstack.runs.run_all(env.id, checkpoint=True)
    cp = kstack.checkpoints.list(env.id, run.id)[0]
    kstack.runner.shutdown()
    res = kstack.checkpoints.resume_from(cp.id)
    assert [kstack.cell(c.id).status for c in cells] == [CellStatus.SUCCESS, CellStatus.STALE, CellStatus.STALE]
    assert res.stale_cells == [cells[1].id, cells[2].id]


def test_cell_edited_since_checkpoint_is_reported_and_stale(kstack):
    env, cells = setup_rng_notebook(kstack, "edit")
    run = kstack.runs.run_all(env.id, checkpoint=True)
    cp = kstack.checkpoints.list(env.id, run.id)[1]
    kstack.notebook.apply_edit(cells[0].id, "import random\nx = [0]", Author.AGENT)
    res = kstack.checkpoints.resume_from(cp.id)
    assert any("edited or added since the checkpoint" in m for m in res.messages)
    assert [kstack.cell(c.id).status for c in cells] == [CellStatus.STALE] * 3


def test_cells_without_declared_outputs_are_called_out(kstack):
    env = kstack.new_ready_env()
    cells = kstack.add_cells(env, "hidden = 3")
    cells += kstack.add_cells(env, "shown = 4", declared_outputs=["shown"])
    run = kstack.runs.run_all(env.id, checkpoint=True)
    cp = kstack.checkpoints.list(env.id, run.id)[1]
    res = kstack.checkpoints.resume_from(cp.id)
    assert any("declares no outputs" in m for m in res.messages)


def test_no_declared_outputs_at_all_is_stated(kstack):
    env = kstack.new_ready_env()
    kstack.add_cells(env, "q = 1")
    run = kstack.runs.run_all(env.id, checkpoint=True)
    cp = kstack.checkpoints.list(env.id, run.id)[0]
    assert cp.saved == [] and any("no cell declared any output" in n for n in cp.notes)


def test_tampered_checkpoint_is_refused_before_anything_is_loaded(kstack):
    env, cells = setup_rng_notebook(kstack, "tamper")
    run = kstack.runs.run_all(env.id, checkpoint=True)
    cp = kstack.checkpoints.list(env.id, run.id)[0]
    evil = Path(cp.path) / "var_x.bin"
    evil.write_bytes(b"cos\nsystem\n(S'touch /tmp/pwned_by_checkpoint'\ntR.")  # malicious pickle
    kstack.runner.shutdown()
    with pytest.raises(CheckpointError, match="corrupted or was modified"):
        kstack.checkpoints.resume_from(cp.id)
    assert not Path("/tmp/pwned_by_checkpoint").exists()
    assert kstack.runner.get_session(env.id) is None  # no kernel was even started


def test_host_code_never_deserializes_anything():
    """'Never load pickles outside the kernel/sandbox process': enforce it structurally."""
    import re
    root = Path(__file__).resolve().parents[1] / "paper_env"
    bad = re.compile(r"^\s*(import|from)\s+(pickle|dill|cloudpickle|joblib|marshal|shelve)\b|\bpickle\.loads?\(|\bdill\.loads?\(", re.M)
    offenders = [p.relative_to(root).as_posix() for p in root.rglob("*.py")
                 if "kernel/runtime" not in p.as_posix() and bad.search(p.read_text())]
    assert offenders == []


def test_resume_replaces_old_kernel_and_never_reuses_in_memory_state(kstack):
    env, cells = setup_rng_notebook(kstack, "fresh")
    run = kstack.runs.run_all(env.id, checkpoint=True)
    cp = kstack.checkpoints.list(env.id, run.id)[0]
    old = kstack.runner.get_session(env.id)
    kstack.runs.run_cell(kstack.add_cells(env, "leftover = 'from the old kernel'")[0].id)
    res = kstack.checkpoints.resume_from(cp.id)
    assert res.session_id != old.id
    (probe,) = kstack.add_cells(env, "print('leftover' in globals(), 'x' in globals())")
    out = kstack.cell_runs(kstack.runs.run_cell(probe.id))[0].outputs[0]["text"]
    assert out == "False True\n"


def test_checkpoints_roundtrip_through_both_repos(uow_factory):
    from paper_env.domain import Checkpoint, UnserializableItem
    cp = Checkpoint(paper_env_id="p", run_id="r", cell_id="c", cell_version_id="v", path="/x", saved=["a"],
                    unserializable=[UnserializableItem(name="g", reason="gen")], rng_saved=True, cell_position=2,
                    seed=9, notebook_version=4, cell_versions={"c": "v"}, file_hashes={"f": "h"}, notes=["n"])
    with uow_factory() as u:
        u.checkpoints.add(cp)
    with uow_factory() as u:
        assert u.checkpoints.get(cp.id) == cp
