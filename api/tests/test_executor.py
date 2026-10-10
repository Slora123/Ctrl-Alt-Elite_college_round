import sys

import pytest

from crisisgrid_api.worker.executor import SubprocessNotebookExecutor

EX = SubprocessNotebookExecutor()


def run(cells, seed=0, timeout=30, secrets=None, executor=None):
    logs: list[str] = []
    res = (executor or EX).run({"cells": [{"kind": "code", "source": c} if isinstance(c, str) else c for c in cells]},
                               seed=seed, timeout_s=timeout, on_log=logs.append)
    return res, logs


def test_runs_cells_top_to_bottom_in_one_shared_namespace_and_reports_metrics():
    res, logs = run(["x = 21", "y = x * 2\nprint('y is', y)", "report_metric('top1', y + 0.5)\nreport_metric('loss', 0.1)"])
    assert res.status == "succeeded" and res.result["metrics"] == {"top1": 42.5, "loss": 0.1} and res.result["cells_run"] == 3
    assert "y is 42" in logs and res.result["hardware_info"]["python"] == sys.version.split()[0]
    assert len(res.result["lockfile_digest"]) == 64


def test_markdown_cells_are_skipped():
    res, _ = run([{"kind": "markdown", "source": "this is ( not python"}, "report_metric('a', 1)"])
    assert res.status == "succeeded" and res.result["metrics"] == {"a": 1.0}


def test_seed_is_applied_and_runs_are_reproducible():
    cell = "import random\nreport_metric('r', random.random())"
    a, _ = run([cell], seed=7)
    b, _ = run([cell], seed=7)
    c, _ = run([cell], seed=8)
    assert a.result["metrics"] == b.result["metrics"] != c.result["metrics"]
    assert a.result["seeded"]["python"] is True


def test_a_raising_cell_is_a_failed_run_not_an_infra_failure():
    res, _ = run(["report_metric('a', 1)", "raise ValueError('bad shape (3, 4)')", "report_metric('never', 1)"])
    assert res.status == "failed" and "ValueError: bad shape" in res.error
    assert res.result["error"]["cell_index"] == 1 and res.result["cells_run"] == 1 and "never" not in res.result["metrics"]
    assert "Traceback" in res.result["error"]["traceback"]


def test_system_exit_in_user_code_is_a_failure_not_a_pass():
    res, _ = run(["import sys\nsys.exit(0)"])
    assert res.status == "failed" and "SystemExit" in res.error


@pytest.mark.parametrize("cell", ["!pip install numpy", "%pip install numpy", "import os\nos.system('x')\n!uv pip install torch", "%matplotlib inline"])
def test_magics_and_installs_inside_cells_are_refused(cell):
    res, _ = run([cell])
    assert res.status == "failed" and "Env manager" in res.error


def test_timeout_kills_the_process():
    res, _ = run(["while True:\n    pass"], timeout=2)
    assert res.status == "timeout" and "2s" in res.error


def test_dying_process_is_an_infrastructure_failure():
    res, _ = run(["import os\nos._exit(137)"])
    assert res.status == "infra_failed" and "137" in res.error


def test_empty_payload_is_infra_failed():
    res = EX.run({"cells": []}, seed=0, timeout_s=5, on_log=lambda s: None)
    assert res.status == "infra_failed"


def test_worker_environment_is_not_visible_to_user_code(monkeypatch):
    monkeypatch.setenv("CG_API_TOKEN", "super-secret-worker-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value")
    res, logs = run(["import os\nprint('TOKEN=', os.environ.get('CG_API_TOKEN'), os.environ.get('AWS_SECRET_ACCESS_KEY'))"])
    assert res.status == "succeeded" and "TOKEN= None None" in logs


def test_secrets_and_credential_shapes_are_scrubbed_from_logs_and_errors():
    ex = SubprocessNotebookExecutor(secrets=["my-dataset-password"])
    res, logs = run(["print('pw my-dataset-password and hf_' + 'a'*30)", "raise RuntimeError('leaked my-dataset-password')"], executor=ex)
    text = "\n".join(logs) + str(res.error) + str(res.result["error"])
    assert "my-dataset-password" not in text and "hf_aaaa" not in text and "[REDACTED]" in text


def test_numpy_is_seeded_when_available():
    pytest.importorskip("numpy")
    cell = "import numpy as np\nreport_metric('n', float(np.random.rand()))"
    a, _ = run([cell], seed=3)
    b, _ = run([cell], seed=3)
    assert a.result["metrics"] == b.result["metrics"] and a.result["seeded"]["numpy"] is True
