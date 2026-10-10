import time

import pytest

from paper_env.backends.registry import BackendRegistry, RuntimeProfile, RuntimeUnavailable, as_registry, default_registry
from paper_env.domain import CellStatus, EventType, SessionLimits
from paper_env.services.limits import SessionReaper
from paper_env.services.paper_envs import PaperEnvService


def test_default_registry_lists_every_profile_with_honest_availability():
    reg = default_registry(object())  # type: ignore[arg-type]
    byid = {p.id: p for p in reg.profiles()}
    assert set(byid) == {"local-cpu", "local-gpu", "tpu", "aws-gpu"}
    assert byid["local-cpu"].available and reg.backend_for("local-cpu") is reg.default
    for pid in ("tpu", "aws-gpu"):
        assert not byid[pid].available and byid[pid].reason
        with pytest.raises(RuntimeUnavailable, match="not available"):
            reg.backend_for(pid)
    with pytest.raises(RuntimeUnavailable, match="unknown"):
        reg.profile("nope")


def test_bare_backend_is_wrapped_into_a_single_profile_registry():
    sentinel = object()
    reg = as_registry(sentinel)
    assert reg.default is sentinel and [p.id for p in reg.profiles()] == ["local-cpu"]
    assert as_registry(reg) is reg


def test_unavailable_profile_cannot_start_a_session(kstack_factory):
    st = kstack_factory()
    env = st.new_ready_env()
    reg = BackendRegistry("local-cpu")
    reg.register(RuntimeProfile("local-cpu", "CPU", "cpu", "local"), st.backend)
    reg.register(RuntimeProfile("tpu", "TPU", "tpu", "tpu", available=False, reason="no TPU backend"), None)
    st.runner.registry = reg
    PaperEnvService(st.uow, st.envs).set_runtime(env.id, "tpu")
    (c,) = st.add_cells(env, "x = 1")
    run = st.runs.run_cell(c.id)
    assert run.status.value == "infra_failed" and "no TPU backend" in run.message
    assert st.cell_runs(run) == []


def test_session_info_reports_runtime_age_and_idle(kstack):
    env = kstack.new_ready_env()
    assert kstack.runner.session_info(env.id) is None
    (c,) = kstack.add_cells(env, "x = 1")
    kstack.runs.run_cell(c.id)
    info = kstack.runner.session_info(env.id)
    assert info["alive"] and info["runtime"] == "local-cpu" and info["busy"] is False and info["cost_so_far"] == 0
    assert 0 <= info["idle_s"] < 30


def _env_with_limits(st, **limits):
    env = st.new_ready_env()
    PaperEnvService(st.uow, st.envs).set_limits(env.id, SessionLimits(**limits))
    a, b = st.add_cells(env, "v = 1", "print(v)")
    st.runs.run_all(env.id)
    return env, (a, b)


def test_idle_timeout_disconnects_marks_cells_stale_and_emits_event(kstack):
    env, cells = _env_with_limits(kstack, idle_timeout_s=60, max_session_s=None, max_cost=None)
    reaper = SessionReaper(kstack.uow, kstack.runner, interval_s=0)
    assert reaper.sweep() == []  # not idle yet
    s = kstack.runner.get_session(env.id)
    pid = s.handle.info["pid"]
    s.last_activity -= 120
    assert reaper.sweep() == [(env.id, "idle")]
    assert kstack.runner.get_session(env.id) is None
    assert [kstack.cell(c.id).status for c in cells] == [CellStatus.STALE, CellStatus.STALE]
    ev = [e for e in kstack.captured if e.type == EventType.SESSION_EXPIRED][0]
    assert ev.payload["reason"] == "idle" and ev.payload["state_lost"] is True and set(ev.payload["stale_cells"]) == {c.id for c in cells}
    import os
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):  # the kernel process is really gone
        for _ in range(20):
            os.kill(pid, 0)
            time.sleep(0.2)
        raise AssertionError("kernel survived")
    # running again just starts a fresh kernel (state from before is gone, and the notebook says so)
    assert kstack.runs.run_all(env.id).status.value == "success"


def test_max_session_length_and_disabled_limits(kstack):
    env, _ = _env_with_limits(kstack, idle_timeout_s=None, max_session_s=100, max_cost=None)
    reaper = SessionReaper(kstack.uow, kstack.runner, interval_s=0)
    s = kstack.runner.get_session(env.id)
    s.last_activity -= 10_000  # idle forever, but the idle limit is disabled
    assert reaper.sweep() == []
    s.started_mono -= 101
    assert reaper.sweep() == [(env.id, "max_session")]


def test_cost_cap_uses_the_runtime_profiles_price(kstack):
    env = kstack.new_ready_env()
    reg = BackendRegistry("paid")
    reg.register(RuntimeProfile("paid", "Paid GPU", "gpu", "local", cost_per_hr=3600.0), kstack.backend)  # 1 unit per second
    kstack.runner.registry = reg
    PaperEnvService(kstack.uow, kstack.envs).set_runtime(env.id, "paid")
    PaperEnvService(kstack.uow, kstack.envs).set_limits(env.id, SessionLimits(idle_timeout_s=None, max_session_s=None, max_cost=5.0))
    (c,) = kstack.add_cells(env, "x = 1")
    kstack.runs.run_cell(c.id)
    reaper = SessionReaper(kstack.uow, kstack.runner, interval_s=0)
    s = kstack.runner.get_session(env.id)
    assert s.cost_per_hr == 3600.0 and reaper.sweep() == []
    s.started_mono -= 10  # 10 s * 1/s = 10 > cap 5
    assert reaper.sweep() == [(env.id, "cost_cap")]


def test_busy_session_is_not_idle_but_does_hit_the_hard_limits(kstack):
    import threading
    env = kstack.new_ready_env()
    PaperEnvService(kstack.uow, kstack.envs).set_limits(env.id, SessionLimits(idle_timeout_s=1, max_session_s=3600, max_cost=None))
    (c,) = kstack.add_cells(env, "import time\ntime.sleep(60)")
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", kstack.runs.run_cell(c.id)))
    th.start()
    end = time.time() + 30
    while time.time() < end and not (kstack.runner.session_info(env.id) or {}).get("busy"):
        time.sleep(0.1)
    reaper = SessionReaper(kstack.uow, kstack.runner, interval_s=0)
    kstack.runner.get_session(env.id).last_activity -= 100
    assert reaper.sweep() == []  # a running cell is not "idle"
    kstack.runner.get_session(env.id).started_mono -= 4000
    assert reaper.sweep() == [(env.id, "max_session")]  # but the maximum length interrupts it
    th.join(60)
    assert out["r"].status.value in ("cancelled", "infra_failed", "failed")


def test_session_for_a_deleted_env_is_shut_down_by_the_sweeper(kstack):
    env, _ = _env_with_limits(kstack, idle_timeout_s=None, max_session_s=None, max_cost=None)
    with kstack.uow() as u:
        u.paper_envs.delete(env.id)
    SessionReaper(kstack.uow, kstack.runner, interval_s=0).sweep()
    assert kstack.runner.get_session(env.id) is None


def test_limits_roundtrip_through_both_repos(uow_factory):
    from paper_env.domain import EnvSpec, PaperEnv
    spec = EnvSpec()
    env = PaperEnv(alias="l", paper_hash="h", env_spec_id=spec.id, owner_id="u1", runtime="local-gpu",
                   limits=SessionLimits(idle_timeout_s=None, max_session_s=99, max_cost=1.5))
    with uow_factory() as u:
        u.paper_envs.add(env, spec)
    with uow_factory() as u:
        got = u.paper_envs.get(env.id)
    assert (got.owner_id, got.runtime, got.limits) == ("u1", "local-gpu", SessionLimits(idle_timeout_s=None, max_session_s=99, max_cost=1.5))


def test_runtime_change_over_http_ends_the_kernel(tmp_path, kernel_template):
    from fastapi.testclient import TestClient
    from paper_env.api import create_api
    from paper_env.config import Settings
    from paper_env.container import build_app
    from paper_env.repos.memory import memory_uow_factory
    import shutil
    core = build_app(Settings(data_dir=tmp_path / "d", reaper_interval_s=0, min_free_disk_bytes=0), uow_factory=memory_uow_factory())
    with TestClient(create_api(core)) as c:
        rts = {r["id"]: r for r in c.get("/runtimes").json()}
        assert rts["local-cpu"]["available"] and not rts["tpu"]["available"] and "TPU" in rts["tpu"]["reason"] or rts["tpu"]["reason"]
        pid = c.post("/paper-envs", json={"alias": "rt", "paper_hash": "h"}).json()["paper_env"]["id"]
        shutil.copytree(kernel_template["venv"], core.settings.venv_dir(pid), symlinks=True)
        with core.uow() as u:
            e = u.paper_envs.get(pid); sp = u.paper_envs.get_spec(e.env_spec_id); sp.lockfile_hash = kernel_template["lockfile_hash"]; u.paper_envs.update_spec(sp)
        c.post(f"/paper-envs/{pid}/cells", json={"source": "x = 1"})
        assert c.post("/runs", json={"paper_env_id": pid, "mode": "all", "wait": True}).json()["status"] == "success"
        assert c.get(f"/paper-envs/{pid}/session").json()["session"]["alive"]
        assert c.put(f"/paper-envs/{pid}/runtime", json={"runtime": "tpu"}).status_code == 409   # refused with the reason
        assert c.put(f"/paper-envs/{pid}/runtime", json={"runtime": "nope"}).status_code == 409
        assert c.get(f"/paper-envs/{pid}/session").json()["session"]["alive"]                      # untouched by the refusal
        r = c.put(f"/paper-envs/{pid}/runtime", json={"runtime": "local-cpu"})
        assert r.status_code == 200
        assert c.get(f"/paper-envs/{pid}/session").json()["session"] is None                      # runtime change ends the kernel
        assert c.put(f"/paper-envs/{pid}/limits", json={"idle_timeout_s": 600, "max_session_s": 7200, "max_cost": 2.5}).json() == \
            {"idle_timeout_s": 600, "max_session_s": 7200, "max_cost": 2.5}
        assert c.put(f"/paper-envs/{pid}/limits", json={"idle_timeout_s": 1}).status_code == 422   # below the sane minimum
        types = [e["type"] for e in c.get("/events/history").json()]
        assert "session_expired" in types
        core.shutdown()
