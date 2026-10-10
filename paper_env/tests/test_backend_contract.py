"""Contract any ComputeBackend must satisfy; run here against LocalBackend."""
import inspect
import shutil

import pytest

from paper_env.backends import (
    BackendError, ComputeBackend, ExecDisplay, ExecError, ExecFinished, ExecInfraFailure, ExecOutput, LocalBackend,
)
from paper_env.backends.base import CheckpointRef
from paper_env.domain import EnvSpec


@pytest.fixture
def be(kstack):
    env = kstack.new_ready_env()
    h = kstack.backend.provision(kstack.spec(env), paper_env_id=env.id, seed=1)
    yield kstack.backend, h, kstack, env
    kstack.backend.teardown(h)


def test_protocol_members_present():
    for name in ("provision", "execute", "interrupt", "snapshot", "teardown", "capabilities", "restore"):
        assert callable(getattr(LocalBackend, name))
    assert isinstance(LocalBackend(__import__("paper_env.config", fromlist=["Settings"]).Settings()), object)
    assert {"gpu", "network", "max_runtime", "cost_per_hr"} <= set(LocalBackend(
        __import__("paper_env.config", fromlist=["Settings"]).Settings()).capabilities())


def test_execute_streams_typed_events(be):
    b, h, *_ = be
    evs = list(b.execute(h, "print('o'); import sys; print('e', file=sys.stderr); 3", 30))
    kinds = [type(e) for e in evs]
    assert ExecOutput in kinds and ExecDisplay in kinds and kinds[-1] is ExecFinished and evs[-1].status == "ok"
    err = list(b.execute(h, "1/0", 30))
    assert any(isinstance(e, ExecError) and e.ename == "ZeroDivisionError" for e in err) and err[-1].status == "error"


def test_dead_runtime_reports_infrastructure_failure_not_exception(be):
    b, h, *_ = be
    list(b.execute(h, "import os; os._exit(2)", 30))
    after = list(b.execute(h, "1", 5))
    assert len(after) == 1 and isinstance(after[0], ExecInfraFailure) and "state is lost" in after[0].message


def test_provision_without_env_is_a_backend_error(kstack):
    env = kstack.new_paper_env(alias="noenv")
    with pytest.raises(BackendError):
        kstack.backend.provision(kstack.spec(env), paper_env_id=env.id, seed=0)


def test_snapshot_restore_roundtrip_via_interface(be, tmp_path):
    b, h, st, env = be
    list(b.execute(h, "import random; keep = [1, 2, 3]; sock = (i for i in range(2))", 30))
    ref = b.snapshot(h, dest=tmp_path / "cp", names=["keep", "sock"])
    assert ref.saved == ["keep"] and ref.unserializable[0]["name"] == "sock" and ref.rng_saved
    b.teardown(h)
    h2 = b.provision(st.spec(env), paper_env_id=env.id, seed=99)
    rep = b.restore(h2, CheckpointRef(id="x", path=ref.path))
    assert rep["restored"] == ["keep"] and rep["failed"] == []
    b.teardown(h2)


def test_teardown_is_idempotent_and_removes_files(be):
    b, h, st, _ = be
    b.teardown(h)
    b.teardown(h)
    assert not (st.settings.kernels_dir() / h.id).exists()


def test_services_do_not_import_backend_internals():
    import paper_env.kernel.runner as r, paper_env.services.runs as s, paper_env.services.checkpoints as c
    for m in (r, s, c):
        src = inspect.getsource(m)
        assert "LocalBackend" not in src and "jupyter_client" not in src
