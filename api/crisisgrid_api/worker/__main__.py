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
    ap.add_argument("--executor", choices=["subprocess", "paper_env", "colab"], default="subprocess",
                    help="paper_env runs jobs in managed uv environments with real Jupyter kernels (needs the paper_env package)")
    ap.add_argument("--paper-env-dir", default="./worker_paper_env", help="where the paper_env executor keeps environments")
    ap.add_argument("--colab-gpu", default="T4", help="colab executor: T4, L4, G4, H100, A100, or 'none' for a CPU runtime")
    ap.add_argument("--colab-tpu", default=None, help="colab executor: v5e1 or v6e1 (instead of a GPU)")
    ap.add_argument("--colab-auth", choices=["oauth2", "adc"], default="oauth2", help="how the colab CLI logs in (log in once by hand first)")
    ap.add_argument("--colab-high-mem", action="store_true", help="colab executor: high-RAM machine (needs Colab Pro)")
    ap.add_argument("--colab-idle-stop", type=int, default=600, help="release the Colab VM after this many idle seconds (0 = never)")
    ap.add_argument("--max-runtime", type=int, default=3600)
    ap.add_argument("--poll", type=float, default=2.0)
    a = ap.parse_args()

    colab = None
    if a.executor == "colab":  # check the login BEFORE registering, so a missing login is one clear message and not failed jobs
        from .colab_executor import ColabError, ColabExecutor

        gpu = None if (a.colab_tpu or a.colab_gpu.lower() == "none") else a.colab_gpu
        try:
            colab = ColabExecutor(a.paper_env_dir, gpu=gpu, tpu=a.colab_tpu, auth=a.colab_auth, high_mem=a.colab_high_mem,
                                  idle_stop_s=a.colab_idle_stop)
            colab.check_login()
        except ColabError as e:
            raise SystemExit(f"[colab worker] {e}") from e
        a.gpu = bool(gpu or a.colab_tpu)
        a.max_runtime = min(a.max_runtime, 3600 * 3)
    api = ApiClient(httpx.Client(base_url=a.api, timeout=30), token=a.token)
    if not a.token:
        # no Docker here, so this worker honestly advertises lower isolation
        caps = Capabilities(gpu=a.gpu, max_runtime_s=a.max_runtime, isolation="remote_vm" if colab else "subprocess",
                            python_versions=[".".join(platform.python_version_tuple()[:2])])
        wid = api.register(a.name, caps, a.registration_token)
        print(f"registered worker {wid}; token (save it, shown once): {api.token}")
    if colab is not None:
        executor: Executor = colab
    elif a.executor == "paper_env":
        from .paper_env_executor import PaperEnvExecutor

        executor = PaperEnvExecutor(a.paper_env_dir)
    else:
        executor = SubprocessNotebookExecutor()
    try:
        Worker(api, executor).run_forever(a.poll)
    finally:
        if colab is not None:
            colab.close()  # release the VM


if __name__ == "__main__":
    main()
