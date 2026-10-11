"""The Colab executor, end to end through the real pipeline, against a stand-in `colab` command (the real one needs a Google login
and a rented VM). The stand-in implements the same sub-commands and runs the exec'd script locally."""
import os
import stat
import sys
from pathlib import Path

import pytest
from test_pipeline_api import NO_GAPS, PUBLIC, Stack, parsed
from test_tier2_execution import BAD_CELL, GOOD, drain, make_worker, start

from crisisgrid_api.worker.colab_executor import ColabError, ColabExecutor
from crisisgrid_core.jobs import Capabilities

FAKE = f"""#!{sys.executable}
import os, subprocess, sys
a = sys.argv[1:]
while a and a[0].startswith("--"):
    a = a[2:] if a[0] == "--config" else a[1:]
log = os.environ["FAKE_COLAB_LOG"]
open(log, "a").write(a[0] + "\\n")
mode = os.environ.get("FAKE_COLAB_MODE", "")
if a[0] == "sessions":
    sys.exit(1 if mode == "nologin" else 0)
if a[0] == "new":
    if mode == "unauth":
        print("401 Unauthorized"); sys.exit(1)
    sys.exit(0)
if a[0] == "exec":
    f = a[a.index("-f") + 1]
    sys.exit(subprocess.run([sys.executable, f]).returncode)
sys.exit(0)  # status, install, restart-kernel, stop
"""


@pytest.fixture
def db(migrated_url):
    return migrated_url


@pytest.fixture
def colab(tmp_path, monkeypatch):
    exe = tmp_path / "colab"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_COLAB_LOG", str(log))
    monkeypatch.delenv("FAKE_COLAB_MODE", raising=False)
    ex = ColabExecutor(tmp_path / "w", gpu="T4", binary=str(exe), idle_stop_s=0)
    ex.calls = lambda: log.read_text().split() if log.exists() else []
    return ex


def test_a_run_goes_through_colab_and_the_report_says_so(db, colab):
    s = Stack(db, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    w = make_worker(s, colab, Capabilities(gpu=True, isolation="remote_vm"))
    run_id = start(s, pid, GOOD, seeds=[0, 1, 2])
    drain(w)
    colab.close()

    v = s.client.get(f"/papers/{pid}/claims").json()[0]["verdict"]
    assert v["status"] == "reproduced" and v["n_seeds"] == 3  # metrics came back from the "VM" and were verified as usual
    calls = colab.calls()
    assert calls.count("new") == 1 and calls.count("restart-kernel") == 3 and calls.count("exec") == 3  # one VM, a fresh kernel per seed
    assert calls[-1] == "stop"
    md = s.client.get(f"/reports/{pid}").text
    assert "remote cloud VM (Google Colab)" in md and "restricted subprocess" not in md
    logs = [e.payload["line"] for e in s.container.events.list(run_id) if e.kind == "job_log"]
    assert any(line.startswith("acc 91.") for line in logs) and not any("@@CG_RESULT@@" in line for line in logs)
    runs = s.client.get(f"/papers/{pid}/graph").json()["current"]["runs"]
    assert runs[0]["ran_on"] and runs[0]["n_ok"] == 3  # the graph pages show where it ran


def test_failures_are_classified_not_confused(colab, monkeypatch):
    noop = lambda _line: None  # noqa: E731
    bad = colab.run({"cells": BAD_CELL["cells"]}, seed=0, timeout_s=60, on_log=noop)
    assert bad.status == "failed" and "RuntimeError" in (bad.error or "")  # the code failed: a fact about the paper's code

    monkeypatch.setenv("FAKE_COLAB_MODE", "unauth")
    colab.close()
    infra = colab.run({"cells": GOOD["cells"]}, seed=0, timeout_s=60, on_log=noop)
    assert infra.status == "infra_failed" and "Log in to Colab" in (infra.error or "")  # Colab's problem: never evidence

    monkeypatch.setenv("FAKE_COLAB_MODE", "nologin")
    with pytest.raises(ColabError, match="not logged in"):
        colab.check_login()


def test_bad_gpu_name_is_refused_instead_of_silently_becoming_an_a100(tmp_path):
    with pytest.raises(ColabError, match="unsupported GPU"):
        ColabExecutor(tmp_path, gpu="RTX4090", binary=sys.executable)
