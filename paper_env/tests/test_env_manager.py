import pytest

from paper_env.domain import EventType
from paper_env.env.manager import InvalidSpec, lockfile_hash, normalize_freeze, parse_failure, validate_requirement
from paper_env.hooks import Decision
from tests.conftest import make_wheel


def test_create_env_builds_venv_lockfile_and_hash(stack, wheels):
    env = stack.new_paper_env(requirements=["demo_a"])
    res = stack.envs.create_env(env.id)
    assert res.success, res
    spec = stack.spec(env)
    assert spec.lockfile_hash == res.lockfile_hash and len(spec.lockfile_hash) == 64
    lock = stack.envs.get_lockfile(env.id)
    assert lock.hash == spec.lockfile_hash
    assert "demo-a==1.0" in lock.text and "demo-b==1.0" in lock.text
    assert lock.text.startswith("# python==")
    assert stack.envs.python_path(env.id).exists()
    assert EventType.ENV_CHANGED in stack.event_types()


def test_lockfile_hash_is_deterministic_across_envs(stack_factory, wheels):
    hashes = []
    for alias in ("a", "b"):
        st = stack_factory()
        env = st.new_paper_env(alias=alias + str(len(hashes)), requirements=["demo_c"])
        hashes.append(st.envs.create_env(env.id).lockfile_hash)
    assert hashes[0] == hashes[1]


def test_hash_changes_when_packages_change(stack, wheels):
    env = stack.new_paper_env()
    h0 = stack.envs.create_env(env.id).lockfile_hash
    r = stack.envs.install_package(env.id, "demo_c", "1.0")
    assert r.success and r.lockfile_hash != h0
    assert stack.spec(env).lockfile_hash == r.lockfile_hash
    assert "demo_c==1.0" in stack.spec(env).requirements
    changed = [e for e in stack.captured if e.type == EventType.ENV_CHANGED][-1]
    assert changed.payload["before_lockfile_hash"] == h0
    assert changed.payload["after_lockfile_hash"] == r.lockfile_hash


def test_pin_version_records_pin_and_survives_rebuild(stack, wheels):
    env = stack.new_paper_env(requirements=["demo_b"])
    stack.envs.create_env(env.id)
    r = stack.envs.pin_version(env.id, "demo_b", "1.0")
    assert r.success and stack.spec(env).pins == {"demo_b": "1.0"}
    rb = stack.envs.rebuild_env(env.id)
    assert rb.success and rb.lockfile_hash == r.lockfile_hash
    assert "demo-b==1.0" in stack.envs.get_lockfile(env.id).text


def test_resolver_conflict_is_structured_and_spec_untouched(stack, wheels):
    env = stack.new_paper_env(requirements=["demo_a"])  # demo_a needs demo_b<2
    ok = stack.envs.create_env(env.id)
    r = stack.envs.install_package(env.id, "demo_b", "2.0")
    assert r.status == "failed" and not r.success
    assert r.conflict_details and "demo" in r.conflict_details
    assert r.failing_package == "demo_b" and "unsatisfiable" in r.conflict_details
    assert stack.spec(env).lockfile_hash == ok.lockfile_hash  # unchanged
    assert "demo_b==2.0" not in stack.spec(env).requirements
    assert EventType.ENV_INSTALL_FAILED in stack.event_types()
    assert (stack.settings.logs_dir(env.id)).exists()


def test_missing_package_reports_failure_not_success(stack, wheels):
    env = stack.new_paper_env()
    stack.envs.create_env(env.id)
    r = stack.envs.install_package(env.id, "does_not_exist_pkg", "9.9")
    assert r.status == "failed" and r.failing_package and "does" in r.failing_package.replace("-", "_")
    assert r.log_path


def test_failed_rebuild_keeps_previous_env(stack, wheels):
    env = stack.new_paper_env(requirements=["demo_c"])
    good = stack.envs.create_env(env.id)
    spec = stack.spec(env)
    spec.requirements = ["demo_c", "nonexistent_pkg_zzz"]
    with stack.uow() as u:
        u.paper_envs.update_spec(spec)
    bad = stack.envs.rebuild_env(env.id)
    assert bad.status == "failed"
    assert stack.envs.python_path(env.id).exists()
    assert stack.envs.get_lockfile(env.id).hash == good.lockfile_hash


def test_install_timeout_is_reported(stack, wheels):
    env = stack.new_paper_env()
    stack.envs.create_env(env.id)
    stack.settings.install_timeout_s = 0.001
    r = stack.envs.install_package(env.id, "demo_c")
    assert r.status == "failed" and r.timed_out


def test_install_before_create_fails_explicitly(stack, wheels):
    env = stack.new_paper_env()
    r = stack.envs.install_package(env.id, "demo_c")
    assert r.status == "failed" and "create_env" in r.error


def test_get_lockfile_before_build_raises(stack):
    env = stack.new_paper_env()
    with pytest.raises(FileNotFoundError):
        stack.envs.get_lockfile(env.id)


@pytest.mark.parametrize("name,version", [("--index-url=http://evil", None), ("good", "1;rm -rf"), ("a b", None), ("", None)])
def test_injection_shaped_inputs_rejected(stack, name, version):
    env = stack.new_paper_env()
    with pytest.raises(InvalidSpec):
        stack.envs.install_package(env.id, name, version)


def test_validate_requirement():
    assert validate_requirement("numpy>=1.2") == "numpy>=1.2"
    for bad in ("-r /etc/passwd", "--index-url x", "not a req!!", "/nope/x.whl"):
        with pytest.raises(InvalidSpec):
            validate_requirement(bad)


def test_hook_deny_blocks_install_then_resume_allows(stack_factory, wheels):
    class Deny:
        def check(self, a): return Decision.deny("no installs")

    st = stack_factory(hook=Deny())
    env = st.new_paper_env()
    blocked = st.envs.create_env(env.id)
    assert blocked.status == "blocked" and blocked.blocked_action_id
    assert not st.settings.venv_python(env.id).exists()  # nothing ran
    assert st.spec(env).lockfile_hash is None
    # approve: executes the original create
    res = st.gate.resume_blocked(blocked.blocked_action_id, Decision.allow("ok"))
    assert res.success and st.settings.venv_python(env.id).exists()
    # next op blocked again, then denied for good
    b2 = st.envs.install_package(env.id, "demo_c")
    assert b2.status == "blocked"
    assert st.gate.resume_blocked(b2.blocked_action_id, Decision.deny("still no")) is None
    assert "demo_c" not in " ".join(st.spec(env).requirements)


def test_secrets_in_install_logs_are_scrubbed(stack, wheels, monkeypatch):
    stack.scrubber.add_secrets(["pw-in-index-url-123"])
    env = stack.new_paper_env()
    stack.envs.create_env(env.id)
    monkeypatch.setenv("UV_FIND_LINKS", "https://user:pw-in-index-url-123@127.0.0.1:9/none")
    r = stack.envs.install_package(env.id, "demo_zzz")
    text = open(r.log_path).read() + (r.conflict_details or "") + str(stack.bus.history())
    assert "pw-in-index-url-123" not in text


def test_hardware_info_has_env_python_and_explicit_cuda(stack, wheels):
    env = stack.new_paper_env()
    stack.envs.create_env(env.id)
    hw = stack.envs.hardware_info(env.id)
    assert hw["env_python"]["python_version"].startswith("3.12")
    assert hw["platform"] and "cuda" in hw  # "not_detected" when absent, never silently missing


def test_normalize_and_hash_helpers():
    a = normalize_freeze("# c\nb==1\na==2\n\n")
    assert a == "a==2\nb==1\n"
    assert lockfile_hash(a, "3.12.1") != lockfile_hash(a, "3.12.2")


def test_parse_failure_best_effort_never_guesses():
    assert parse_failure("something unrelated") == (None, None)
    pkg, conflict = parse_failure("x\n  × No solution found when resolving dependencies:\n  ╰─▶ Because there is no version of foo==9 and you require foo==9")
    assert pkg == "foo" and "No solution found" in conflict
