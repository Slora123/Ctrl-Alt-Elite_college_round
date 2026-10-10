"""Offline end-to-end demo and fallback path:  python -m crisisgrid_api.demo

Runs the whole backend in one process with no network and no API keys: a generated paper PDF, a scripted stand-in for the
LLM, a stub for the GitHub lookup, the real pipeline, the real job queue and a REAL worker executing the notebook in a
subprocess, then the Claim verifier. It exists so the platform can always show something true: Tier 1 never needs compute,
and this shows Tier 2 without depending on a model provider.

The scripted "LLM" returns fixed extraction results, so this demonstrates the plumbing and the safety rules, not model quality.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from crisisgrid_core.jobs import Capabilities
from crisisgrid_core.papers import RepoInfo

from .config import Settings
from .container import build_container
from .db.base import make_engine
from .demo_pdf import make_pdf
from .gateway.gateway import Gateway, Provider
from .gateway.testing import ScriptedClient
from .main import create_app
from .worker.client import ApiClient
from .worker.executor import SubprocessNotebookExecutor
from .worker.loop import Worker

PAPER = [[
    "Deep Residual Learning on CIFAR-10 (demo paper)",
    "We train ResNet-20 on CIFAR-10 with a learning rate of 0.1 and a batch size of 128 for 160 epochs using SGD.",
    "Our model achieves 91.25% top-1 accuracy on the test set, averaged over 3 seeds.",
    "Code is available at https://github.com/acme/resnet-cifar and data at https://huggingface.co/datasets/uoft-cs/cifar10.",
]]

NOTEBOOK = {"cells": [
    {"cell_type": "markdown", "source": "# Stand-in for the authors' training script"},
    {"cell_type": "code", "source": "\n".join(["import random", "random.seed(SEED)", "acc = 91.0 + random.random() * 0.5",
                                               "print('final accuracy', round(acc, 3))"])},
    {"cell_type": "code", "source": "report_metric('top-1', acc)"},
]}

PARSED = {
    "title": "Deep Residual Learning on CIFAR-10 (demo paper)",
    "claims": [{"text": "ResNet-20 reaches 91.25% top-1 accuracy on CIFAR-10", "metric": "top1", "dataset": "cifar10", "split": "test",
                "reported_value": 91.25, "unit": "percent", "tolerance": 0.5, "provenance": "stated",
                "source": {"kind": "paper", "ref": "p1", "quote": "Our model achieves 91.25% top-1 accuracy on the test set"}}],
    "hyperparameters": {
        "learning_rate": {"value": 0.1, "source": {"kind": "paper", "ref": "p1", "quote": "a learning rate of 0.1"}},
        "batch_size": {"value": 128, "source": {"kind": "paper", "ref": "p1", "quote": "a batch size of 128"}},
        "epochs": {"value": 160, "source": {"kind": "paper", "ref": "p1", "quote": "for 160 epochs"}},
        "optimizer": {"value": "sgd", "source": {"kind": "paper", "ref": "p1", "quote": "using SGD"}},
        "momentum": {"value": 0.9, "source": {"kind": "paper", "ref": "p1", "quote": "momentum of 0.9"}},  # invented: will be dropped
    },
    "datasets": [{"name": "CIFAR-10", "access": "public", "url": "https://huggingface.co/datasets/uoft-cs/cifar10",
                  "split_provenance": "paper_official"}],
    "metrics": [{"name": "top-1 accuracy", "definition": "top-1 accuracy"}],
    "seeds": {"value": "3 seeds", "source": {"kind": "paper", "ref": "p1", "quote": "averaged over 3 seeds"}},
    "reports_variance": False,
}


class _StubGitHub:
    def fetch(self, url: str) -> RepoInfo:
        return RepoInfo(url=url, commit="demo0000", default_branch="main", dependencies_pinned=True, has_readme=True,
                        default_branch_last_commit=datetime(2020, 1, 1, tzinfo=timezone.utc))


def run_demo(out=sys.stdout) -> str:
    def say(msg: str = "") -> None:
        print(msg, file=out)

    with tempfile.TemporaryDirectory(prefix="cg-demo-", ignore_cleanup_errors=True) as tmp:
        url = f"sqlite:///{(Path(tmp) / 'demo.db').as_posix()}"
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "db" / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")

        llm = ScriptedClient(json.dumps(PARSED), json.dumps({"gaps": []}))
        settings = Settings(database_url=url, reaper_interval_s=0)
        engine = make_engine(url)
        container = build_container(engine, settings, fetcher=_StubGitHub(),
                                    gateway=Gateway([Provider("scripted", "demo-llm", llm)], sleep=lambda s: None))
        client = TestClient(create_app(settings, engine, container))

        say("1. Upload the paper")
        paper = client.post("/papers", content=make_pdf(PAPER), headers={"content-type": "application/pdf"}).json()
        say(f"   paper {paper['paper_id']}  pages={paper['pages']}  sha256={paper['paper_hash'][:12]}...")

        api = ApiClient(client)
        say("2. A worker registers (outbound only) and the pipeline starts; 3 clean runs are queued, one per seed")
        api.register("demo-worker", Capabilities())
        run_id = client.post(f"/papers/{paper['paper_id']}/runs", json={"notebook": NOTEBOOK}).json()["run_id"]
        say(f"   run {run_id}: {client.get(f'/runs/{run_id}').json()['status']}")

        say("3. The worker executes each job in a fresh subprocess with a fixed seed")
        worker = Worker(api, SubprocessNotebookExecutor(), heartbeat_s=1000)
        n = 0
        while worker.run_once():
            n += 1
        state = client.get(f"/runs/{run_id}").json()
        say(f"   {n} job(s) done; run is now: {state['status']} at {state['node']}")

        say("4. The Claim verifier decides (never the model)")
        for c in client.get(f"/papers/{paper['paper_id']}/claims").json():
            v = c["verdict"]
            say(f"   {v['status'].upper():<14} reported {v['reported']}  measured {v['measured_mean']:.3f}  seeds={v['n_seeds']}")

        say("5. A human accepts the final gate")
        gate = client.get("/gates", params={"run_id": run_id}).json()[0]
        client.post(f"/gates/{gate['id']}/respond", json={"action": "accept"})
        say(f"   run {client.get(f'/runs/{run_id}').json()['status']}")

        report = client.get(f"/reports/{paper['paper_id']}").text
        say("\n" + "=" * 78 + "\n" + report + "=" * 78)
        engine.dispose()  # release the SQLite file before the temp directory goes (Windows cannot delete an open file)
        return report


def main() -> int:
    run_demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
