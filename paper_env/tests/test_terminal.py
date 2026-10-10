import os
import sys
import time

import pytest

from paper_env.domain import EventType
from paper_env.services.terminal import TerminalError, TerminalService

pytestmark = pytest.mark.skipif(os.name != "posix", reason="pty terminal is POSIX only")


def read_until(term, needle, t=15):
    buf = ""
    end = time.time() + t
    while time.time() < end and needle not in buf:
        chunk = term.read(0.2)
        if chunk is None:
            break
        buf += chunk
    return buf


@pytest.fixture
def ready(kstack):
    kstack.settings.enable_terminal = True
    env = kstack.new_ready_env()
    return kstack, env, TerminalService(kstack.settings, kstack.scrubber)


def test_disabled_by_default(kstack):
    env = kstack.new_ready_env()
    assert kstack.settings.enable_terminal is False
    with pytest.raises(TerminalError, match="disabled"):
        TerminalService(kstack.settings, kstack.scrubber).open(env.id)


def test_needs_a_built_environment(kstack):
    kstack.settings.enable_terminal = True
    env = kstack.new_paper_env(alias="unbuilt")
    with pytest.raises(TerminalError, match="not been created"):
        TerminalService(kstack.settings, kstack.scrubber).open(env.id)


def test_shell_runs_in_the_workspace_with_the_envs_python_and_scrubbed_environment(ready, monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "super-secret-token-value")
    st, env, svc = ready
    st.scrubber.add_secrets(["typed-secret-value-zz"])
    t = svc.open(env.id)
    try:
        t.write("pwd; which python; echo TOK=[$MY_API_TOKEN]; echo typed-secret-value-zz; echo DONE-$((6*7))\n")
        out = read_until(t, "DONE-42")
        assert str(st.settings.files_dir(env.id)) in out
        assert str(st.settings.venv_dir(env.id) / "bin" / "python") in out
        assert "TOK=[]" in out  # secret-looking variables are not inherited
        assert "typed-secret-value-zz" not in out and "[REDACTED]" in out  # output is scrubbed
        t.resize(30, 100)
        t.write("stty size; exit\n")
        assert "30 100" in read_until(t, "30 100")
    finally:
        t.close()
    assert t.closed and t.proc.poll() is not None


def test_close_kills_the_whole_process_group(ready):
    st, env, svc = ready
    t = svc.open(env.id)
    t.write("sleep 300 &\n echo bg-started\n")
    read_until(t, "bg-started")
    pgid = os.getpgid(t.proc.pid)
    t.close()
    time.sleep(0.5)
    # (killpg(pgid, 0) raises different errors per OS for a dead group, so ask pgrep: exit 1 means no process is left in it)
    import subprocess
    assert subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True).returncode == 1, "the background job survived"


def test_at_most_two_terminals_per_environment_and_close_all(ready):
    st, env, svc = ready
    a, b = svc.open(env.id), svc.open(env.id)
    with pytest.raises(TerminalError, match="at most"):
        svc.open(env.id)
    svc.close_all()
    assert a.closed and b.closed
    svc.open(env.id).close()


def test_packages_installed_in_the_terminal_are_recorded_as_unmanaged_change(ready, wheels):
    st, env, svc = ready
    before = st.spec(env).lockfile_hash
    assert before and st.envs.detect_drift(env.id) in (False, True)  # tolerate the template's own freeze difference
    before = st.spec(env).lockfile_hash
    assert st.envs.detect_drift(env.id) is False  # nothing changed: nothing reported
    t = svc.open(env.id)
    try:
        t.write(f"uv pip install --no-index --find-links {wheels} demo_c==1.0 && echo INSTALLED-$((20+22))\n")
        # the pty echoes what we type, so wait for a computed marker that only appears once the command has finished
        assert "INSTALLED-42" in read_until(t, "INSTALLED-42", 60)
    finally:
        t.close()
    assert st.envs.detect_drift(env.id) is True
    after = st.spec(env).lockfile_hash
    assert after != before
    ev = [e for e in st.captured if e.type == EventType.ENV_CHANGED and e.payload.get("op") == "external_change"][-1]
    assert ev.payload["unmanaged"] is True and ev.payload["before_lockfile_hash"] == before and ev.payload["after_lockfile_hash"] == after
    assert "demo-c==1.0" in st.envs.get_lockfile(env.id).text  # the lockfile on disk was re-captured too
