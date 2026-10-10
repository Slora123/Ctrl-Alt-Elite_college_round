"""Run a worker:  python -m crisisgrid_api.worker --api http://localhost:8000 --name my-laptop [--token cgw_...]"""
from __future__ import annotations

import argparse
import platform

import httpx

from crisisgrid_core.jobs import Capabilities

from .client import ApiClient
from .executor import Executor, SubprocessNotebookExecutor
from .loop import Worker


def main() -> None:
    ap = argparse.ArgumentParser(description="CrisisGrid worker (outbound-only)")
    ap.add_argument("--api", required=True, help="control plane base URL")
    ap.add_argument("--name", default=platform.node() or "worker")
    ap.add_argument("--token", help="existing worker token; omit to register a new worker")
    ap.add_argument("--registration-token", help="shared registration secret, if the API requires one")
    ap.add_argument("--gpu", action="store_true", help="advertise a GPU (not auto-detected)")
    ap.add_argument("--executor", choices=["subprocess", "paper_env"], default="subprocess",
                    help="paper_env runs jobs in managed uv environments with real Jupyter kernels (needs the paper_env package)")
    ap.add_argument("--paper-env-dir", default="./worker_paper_env", help="where the paper_env executor keeps environments")
    ap.add_argument("--max-runtime", type=int, default=3600)
    ap.add_argument("--poll", type=float, default=2.0)
    a = ap.parse_args()

    api = ApiClient(httpx.Client(base_url=a.api, timeout=30), token=a.token)
    if not a.token:
        # no Docker here, so this worker honestly advertises lower isolation
        caps = Capabilities(gpu=a.gpu, max_runtime_s=a.max_runtime, isolation="subprocess",
                            python_versions=[".".join(platform.python_version_tuple()[:2])])
        wid = api.register(a.name, caps, a.registration_token)
        print(f"registered worker {wid}; token (save it, shown once): {api.token}")
    if a.executor == "paper_env":
        from .paper_env_executor import PaperEnvExecutor

        executor: Executor = PaperEnvExecutor(a.paper_env_dir)
    else:
        executor = SubprocessNotebookExecutor()
    Worker(api, executor).run_forever(a.poll)


if __name__ == "__main__":
    main()
