"""Same assertions run against the in-memory fake and the SQLite implementation."""
import pytest

from paper_env.domain import *  # noqa: F403
from paper_env.repos import Conflict, NotFound


def make_env(uow, alias="resnet-2015"):
    spec = EnvSpec(requirements=["numpy"], pins={"numpy": "1.26.4"}, seed=7)
    env = PaperEnv(alias=alias, paper_hash="abc", env_spec_id=spec.id)
    uow.paper_envs.add(env, spec)
    return env, spec


def test_paper_env_roundtrip_and_alias_unique(uow_factory):
    with uow_factory() as u:
        env, spec = make_env(u)
    with uow_factory() as u:
        assert u.paper_envs.get(env.id) == env
        assert u.paper_envs.get_by_alias("resnet-2015").id == env.id
        assert u.paper_envs.get_spec(spec.id) == spec
        assert [e.id for e in u.paper_envs.list()] == [env.id]
    with pytest.raises(Conflict):
        with uow_factory() as u:
            make_env(u)  # duplicate alias
    with uow_factory() as u:
        assert len(u.paper_envs.list()) == 1


def test_update_spec_and_env(uow_factory):
    with uow_factory() as u:
        env, spec = make_env(u)
        spec.lockfile_hash = "deadbeef"
        u.paper_envs.update_spec(spec)
        env.current_notebook_version = 3
        u.paper_envs.update(env)
    with uow_factory() as u:
        assert u.paper_envs.get_spec(spec.id).lockfile_hash == "deadbeef"
        assert u.paper_envs.get(env.id).current_notebook_version == 3


def test_not_found(uow_factory):
    with uow_factory() as u:
        for fn in (u.paper_envs.get, u.cells.get_cell, u.cells.get_version, u.runs.get_run,
                   u.datasets.get, u.checkpoints.get, u.blocked.get):
            with pytest.raises(NotFound):
                fn("nope")
        with pytest.raises(NotFound):
            u.paper_envs.get_by_alias("nope")


def test_cells_ordered_and_versions(uow_factory):
    with uow_factory() as u:
        env, _ = make_env(u)
        c2 = Cell(paper_env_id=env.id, position=1, declared_outputs=["y"])
        c1 = Cell(paper_env_id=env.id, position=0, declared_inputs=["x"], source_span={"line": [1, 3]})
        u.cells.add_cell(c2)
        u.cells.add_cell(c1)
        v1 = CellVersion(cell_id=c1.id, source="x=1", author=Author.HUMAN, content_hash=content_hash("x=1"))
        v2 = CellVersion(cell_id=c1.id, source="x=2", author=Author.AGENT, parent_version_id=v1.id,
                         content_hash=content_hash("x=2"), diff="-x=1\n+x=2")
        u.cells.add_version(v1)
        u.cells.add_version(v2)
    with uow_factory() as u:
        cells = u.cells.list_cells(env.id)
        assert [c.id for c in cells] == [c1.id, c2.id]
        assert cells[0].source_span == {"line": [1, 3]}
        assert [v.id for v in u.cells.list_versions(c1.id)] == [v1.id, v2.id]
        assert u.cells.get_version(v2.id).author == Author.AGENT
        c = u.cells.get_cell(c1.id)
        c.status = CellStatus.STALE
        c.current_version_id = v2.id
        u.cells.update_cell(c)
    with uow_factory() as u:
        c = u.cells.get_cell(c1.id)
        assert c.status == CellStatus.STALE and c.current_version_id == v2.id


def test_runs_and_cell_runs_keep_execution_order(uow_factory):
    with uow_factory() as u:
        env, _ = make_env(u)
        run = Run(paper_env_id=env.id, notebook_version=1, mode=RunMode.CLEAN, seed=1,
                  hardware_info={"python": "3.12"}, verification_eligible=True)
        u.runs.add_run(run)
        crs = [CellRun(run_id=run.id, cell_id=f"c{i}", cell_version_id=f"v{i}") for i in (3, 1, 2)]
        for cr in crs:
            u.runs.add_cell_run(cr)
        run.status = RunStatus.INFRA_FAILED
        run.failure_kind = FailureKind.INFRASTRUCTURE
        u.runs.update_run(run)
        crs[0].error = ErrorInfo(type="ValueError", message="boom", traceback="tb", failing_lineno=2)
        crs[0].status = CellStatus.FAILED
        u.runs.update_cell_run(crs[0])
    with uow_factory() as u:
        got = u.runs.get_run(run.id)
        assert got.verification_eligible and got.failure_kind == FailureKind.INFRASTRUCTURE
        assert got.hardware_info == {"python": "3.12"}
        listed = u.runs.list_cell_runs(run.id)
        assert [c.cell_id for c in listed] == ["c3", "c1", "c2"]
        assert listed[0].error.type == "ValueError"
        assert len(u.runs.list_runs(env.id)) == 1


def test_two_runs_same_notebook_version_never_overwrite(uow_factory):
    with uow_factory() as u:
        env, _ = make_env(u)
        for _ in range(2):
            u.runs.add_run(Run(paper_env_id=env.id, notebook_version=1, mode=RunMode.ALL, seed=1))
    with uow_factory() as u:
        assert len(u.runs.list_runs(env.id)) == 2


def test_datasets_manifest_and_usage(uow_factory):
    m = DatasetManifest(
        dataset_id="cifar10@v1", name="cifar10", version="v1", source_type=SourceType.URL,
        source_uri="http://x/y", files=[DatasetFile(path="a.bin", size=3, sha256="ff")],
        splits=[DatasetSplit(name="train", provenance=SplitProvenance.PAPER_OFFICIAL)],
    )
    with uow_factory() as u:
        u.datasets.upsert(m)
        m2 = m.model_copy(update={"status": DatasetStatus.VERIFIED})
        u.datasets.upsert(m2)  # upsert replaces
        u.datasets.mark_in_use("run1", m.dataset_id)
        u.datasets.mark_in_use("run1", m.dataset_id)  # idempotent
    with uow_factory() as u:
        got = u.datasets.get(m.dataset_id)
        assert got.status == DatasetStatus.VERIFIED
        assert got.splits[0].provenance == SplitProvenance.PAPER_OFFICIAL
        assert got.files[0].sha256 == "ff"
        assert u.datasets.exists(m.dataset_id) and len(u.datasets.list()) == 1
        assert u.datasets.in_use_dataset_ids() == {m.dataset_id}
        u.datasets.release_run("run1")
        assert u.datasets.in_use_dataset_ids() == set()
        u.datasets.delete(m.dataset_id)
        assert not u.datasets.exists(m.dataset_id)


def test_event_store_sequence_and_filters(uow_factory):
    with uow_factory() as u:
        e1 = u.events.append(Event(type=EventType.RUN_STARTED, run_id="r1", paper_env_id="p"))
        e2 = u.events.append(Event(type=EventType.CELL_OUTPUT, run_id="r1", payload={"text": "hi"}))
        e3 = u.events.append(Event(type=EventType.RUN_STARTED, run_id="r2"))
        assert e1.seq < e2.seq < e3.seq
    with uow_factory() as u:
        assert [e.seq for e in u.events.list()] == [e1.seq, e2.seq, e3.seq]
        assert [e.seq for e in u.events.list(after_seq=e1.seq)] == [e2.seq, e3.seq]
        assert [e.seq for e in u.events.list(run_id="r1")] == [e1.seq, e2.seq]
        assert [e.seq for e in u.events.list(types=[EventType.RUN_STARTED])] == [e1.seq, e3.seq]
        assert u.events.list(limit=1)[0].seq == e1.seq
        assert u.events.list(run_id="r1")[1].payload == {"text": "hi"}


def test_checkpoints_and_blocked_actions(uow_factory):
    with uow_factory() as u:
        cp = Checkpoint(paper_env_id="p", run_id="r", cell_id="c", cell_version_id="v", path="/x",
                        saved=["a"], unserializable=[UnserializableItem(name="gen", reason="generator")])
        u.checkpoints.add(cp)
        ba = BlockedAction(kind=ActionKind.INSTALL_PACKAGE, paper_env_id="p", decision="needs_approval",
                           payload={"name": "numpy"})
        u.blocked.add(ba)
    with uow_factory() as u:
        assert u.checkpoints.get(cp.id).unserializable[0].reason == "generator"
        assert len(u.checkpoints.list("p")) == 1 and u.checkpoints.list("p", run_id="zz") == []
        assert [a.id for a in u.blocked.list_pending("p")] == [ba.id]
        ba.status = BlockedActionStatus.ALLOWED
        u.blocked.update(ba)
    with uow_factory() as u:
        assert u.blocked.list_pending() == []


def test_uow_rolls_back_on_error(uow_factory):
    class Boom(Exception): ...
    with pytest.raises(Boom):
        with uow_factory() as u:
            make_env(u)
            raise Boom
    with uow_factory() as u:
        assert u.paper_envs.list() == []


def test_delete_paper_env_removes_children(uow_factory):
    with uow_factory() as u:
        env, spec = make_env(u)
        c = Cell(paper_env_id=env.id, position=0)
        u.cells.add_cell(c)
        u.cells.add_version(CellVersion(cell_id=c.id, source="", author=Author.HUMAN, content_hash="h"))
        r = Run(paper_env_id=env.id, notebook_version=0, mode=RunMode.ALL, seed=0)
        u.runs.add_run(r)
        u.paper_envs.delete(env.id)
    with uow_factory() as u:
        assert u.paper_envs.list() == []
        assert u.cells.list_cells(env.id) == [] and u.cells.list_versions(c.id) == []
        assert u.runs.list_runs(env.id) == []
        with pytest.raises(NotFound):
            u.paper_envs.get_spec(spec.id)
