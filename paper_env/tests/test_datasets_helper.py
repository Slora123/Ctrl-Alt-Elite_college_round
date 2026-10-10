import json
from pathlib import Path

import pytest

from paper_env.domain import (
    CellStatus, DatasetManifest, DatasetSplit, DatasetStatus, EventType, FailureKind, RunStatus, SourceType,
    SplitProvenance,
)


def register_local(st, tmp_path, name="mini", approx=False, content="hello data\n"):
    d = tmp_path / f"src_{name}"
    d.mkdir(exist_ok=True)
    (d / "x.txt").write_text(content)
    st.datasets.register(DatasetManifest(
        dataset_id=f"{name}@v1", name=name, version="v1", source_type=SourceType.LOCAL_PATH, source_uri=str(d),
        license="CC0", splits=[DatasetSplit(name="train", provenance=SplitProvenance.APPROXIMATE if approx else SplitProvenance.PAPER_OFFICIAL)]))
    return f"{name}@v1"


def run(st, env, src, **kw):
    (c,) = st.add_cells(env, src, **kw)
    r = st.runs.run_cell(c.id)
    return c, r, st.cell_runs(r)[0]


def test_resolve_inside_kernel_works_offline_and_is_recorded_on_run(kstack, tmp_path):
    ds = register_local(kstack, tmp_path)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    kstack.online = False
    c, r, cr = run(kstack, env, "path = datasets.resolve('mini@v1')\nprint(type(path).__name__, (path / 'x.txt').read_text().strip())")
    assert r.status == RunStatus.SUCCESS and cr.outputs[0]["text"] == "PosixPath hello data\n"
    assert r.dataset_manifests[0]["dataset_id"] == "mini@v1" and r.dataset_manifests[0]["overall_checksum"]
    assert r.approximate_split is False
    assert kstack.datasets.status("mini@v1") == DatasetStatus.CACHED


def test_helper_info_and_list_expose_no_source_or_credentials(kstack, tmp_path):
    ds = register_local(kstack, tmp_path)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    c, r, cr = run(kstack, env, "import json\nprint(datasets.list())\nprint(json.dumps(datasets.info('mini@v1'), sort_keys=True))")
    lines = cr.outputs[0]["text"].splitlines()
    assert lines[0] == "['mini@v1']"
    info = json.loads(lines[1])
    assert info["license"] == "CC0" and info["files"] == [{"path": "x.txt", "size": 11}]
    assert "source_uri" not in info and "source_type" not in info
    assert str(tmp_path / "src_mini") not in lines[1]


def test_undeclared_dataset_is_an_explicit_experiment_error(kstack, tmp_path):
    register_local(kstack, tmp_path)  # registered but NOT declared on the paper env
    env = kstack.new_ready_env()
    c, r, cr = run(kstack, env, "datasets.resolve('mini@v1')")
    assert cr.failure_kind == FailureKind.EXPERIMENT and cr.error.type == "DatasetUnavailable"
    assert "not available to this run" in cr.error.message


def test_cells_cannot_modify_cached_data(kstack, tmp_path):
    ds = register_local(kstack, tmp_path)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    code = (
        "p = datasets.resolve('mini@v1')\n"
        "for name, mode in (('x.txt', 'a'), ('x.txt', 'w'), ('new.txt', 'w')):\n"
        "    try:\n"
        "        open(p / name, mode).write('hacked')\n"
        "        print('WROTE', name)\n"
        "    except PermissionError:\n"
        "        print('denied', name)\n"
        "import os\n"
        "try:\n"
        "    os.remove(p / 'x.txt'); print('REMOVED')\n"
        "except PermissionError:\n"
        "    print('denied remove')\n"
    )
    c, r, cr = run(kstack, env, code)
    assert cr.outputs[0]["text"].splitlines() == ["denied x.txt", "denied x.txt", "denied new.txt", "denied remove"]
    assert (Path(kstack.datasets.resolve("mini@v1")) / "x.txt").read_text() == "hello data\n"
    assert kstack.datasets.verify("mini@v1").ok


def test_preflight_offline_fails_before_any_cell_executes(kstack, tmp_path):
    kstack.datasets.register(DatasetManifest(dataset_id="remote@v1", name="remote", version="v1",
                                             source_type=SourceType.URL, source_uri="http://example.invalid/data.bin"))
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, ["remote@v1"])
    kstack.online = False
    (c,) = kstack.add_cells(env, "print('should never run')")
    r = kstack.runs.run_cell(c.id)
    assert r.status == RunStatus.INFRA_FAILED and r.failure_kind == FailureKind.INFRASTRUCTURE
    assert "remote@v1" in r.message and "network is unavailable" in r.message
    assert kstack.cell_runs(r) == [] and kstack.runner.get_session(env.id) is None
    assert EventType.CELL_STARTED not in kstack.event_types()
    fin = [e for e in kstack.captured if e.type == EventType.RUN_FINISHED][0]
    assert fin.payload["preflight_problems"][0]["code"] == "offline_remote_only"
    assert kstack.cell(c.id).status == CellStatus.PENDING  # cell untouched


def test_approximate_split_is_flagged_on_run(kstack, tmp_path):
    ds = register_local(kstack, tmp_path, approx=True)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    c, r, cr = run(kstack, env, "x = 1")
    assert r.approximate_split is True and r.status == RunStatus.SUCCESS


def test_dataset_in_use_by_active_run_is_protected_and_released(kstack, tmp_path):
    ds = register_local(kstack, tmp_path)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    run(kstack, env, "x = 1")
    with kstack.uow() as u:
        assert u.datasets.in_use_dataset_ids() == set()  # released when the run finished


# ------------------------------------------------------------------ derived datasets
DERIVE = (
    "out = datasets.derive('clean', 'r1', parent='mini@v1')\n"
    "(out / 'rows.txt').write_text(datasets.resolve('mini@v1').joinpath('x.txt').read_text().upper())\n"
    "print('derived at', out.name)"
)


def test_derived_dataset_registered_linked_and_readable_next_cell(kstack, tmp_path):
    ds = register_local(kstack, tmp_path, approx=True)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    a, b = kstack.add_cells(env, DERIVE, "p = datasets.resolve('clean@r1')\nprint((p / 'rows.txt').read_text().strip())\n"
                                      "try:\n    (p / 'rows.txt').write_text('x')\nexcept PermissionError:\n    print('read-only')")
    r1 = kstack.runs.run_cell(a.id)
    cr_a = kstack.cell_runs(r1)[0]
    assert r1.status == RunStatus.SUCCESS and cr_a.outputs[0]["text"] == "derived at clean@r1\n"
    m = kstack.datasets.get("clean@r1")
    assert m.parent_dataset_id == "mini@v1" and m.run_id == r1.id and m.producing_cell_version_id == cr_a.cell_version_id
    assert m.status == DatasetStatus.VERIFIED and m.files[0].path == "rows.txt"
    assert m.splits[0].provenance == SplitProvenance.APPROXIMATE  # inherited from the parent
    assert str(kstack.settings.runs_dir(r1.id)) in m.cache_path  # lives in the per-run directory
    fin = [e for e in kstack.captured if e.type == EventType.CELL_FINISHED][0]
    assert fin.payload["derived_datasets"] == ["clean@r1"]
    r2 = kstack.runs.run_cell(b.id)  # same kernel, same session: the new dataset is visible
    assert kstack.cell_runs(r2)[0].outputs[0]["text"] == "HELLO DATA\nread-only\n"


def test_derived_data_survives_into_later_runs_and_blocks_eviction(kstack, tmp_path):
    ds = register_local(kstack, tmp_path)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    (a,) = kstack.add_cells(env, DERIVE)
    kstack.runs.run_cell(a.id)
    kstack.runner.shutdown()
    (b,) = kstack.add_cells(env, "print(datasets.list())")
    r = kstack.runs.run_cell(b.id)
    assert kstack.cell_runs(r)[0].outputs[0]["text"] == "['clean@r1', 'mini@v1']\n"


def test_failed_cell_does_not_register_derived_data(kstack, tmp_path):
    ds = register_local(kstack, tmp_path)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    (a,) = kstack.add_cells(env, DERIVE + "\nraise RuntimeError('late failure')")
    kstack.runs.run_cell(a.id)
    assert not kstack.datasets._exists("clean@r1")


def test_derive_rejects_bad_names_and_reuse(kstack, tmp_path):
    ds = register_local(kstack, tmp_path)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    code = (
        "for bad in ('../escape', 'a/b', ''):\n"
        "    try:\n        datasets.derive(bad, 'v1'); print('ACCEPTED', bad)\n"
        "    except ValueError:\n        print('rejected', repr(bad))\n"
        "datasets.derive('ok', 'v1')\n"
        "try:\n    datasets.derive('ok', 'v1')\nexcept FileExistsError:\n    print('immutable')\n"
    )
    c, r, cr = run(kstack, env, code)
    assert cr.outputs[0]["text"].splitlines() == ["rejected '../escape'", "rejected 'a/b'", "rejected ''", "immutable"]


def test_forged_derived_request_cannot_escape_run_directory(kstack, tmp_path):
    """Requests are untrusted: the host recomputes the directory and validates names."""
    ds = register_local(kstack, tmp_path)
    env = kstack.new_ready_env()
    kstack.paper_envs.set_datasets(env.id, [ds])
    code = (
        "import json, os\n"
        "p = os.environ['PAPER_ENV_DATASETS_MANIFEST']\n"
        "run_dir = os.path.dirname(json.load(open(p))['run']['derived_dir'])\n"
        "with open(os.path.join(run_dir, 'derived_requests.jsonl'), 'a') as f:\n"
        "    f.write(json.dumps({'name': '../../evil', 'version': 'v', 'parent': None}) + '\\n')\n"
        "    f.write('not json at all\\n')\n"
        "    f.write(json.dumps({'name': 'ghost', 'version': 'v1', 'parent': 'nope@v1'}) + '\\n')\n"
    )
    c, r, cr = run(kstack, env, code)
    notes = [o["text"] for o in cr.outputs if o["type"] == "note"]
    assert len(notes) == 3 and all("rejected" in n for n in notes)
    assert [m.dataset_id for m in kstack.datasets.list() if m.run_id] == []
    assert r.status == RunStatus.SUCCESS
