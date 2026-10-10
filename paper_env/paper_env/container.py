"""Composition root: the one place that knows which concrete implementations are used."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backends import ComputeBackend, LocalBackend
from .config import Settings
from .datasets import DatasetStore
from .env import EnvManager
from .events import EventBus
from .hooks import ActionGate, AllowAllHook, PreExecuteHook
from .kernel.runner import KernelRunner
from .repos import UnitOfWorkFactory
from .repos.sqlite import sqlite_uow_factory
from .security import EnvVarSecretsProvider, Scrubber, SecretsProvider
from .services.checkpoints import CheckpointService
from .services.files import FileService
from .services.notebook import NotebookService
from .services.paper_envs import PaperEnvService
from .services.runs import RunService


@dataclass
class App:
    settings: Settings
    uow: UnitOfWorkFactory
    scrubber: Scrubber
    secrets: SecretsProvider
    bus: EventBus
    gate: ActionGate
    envs: EnvManager
    datasets: DatasetStore
    backend: ComputeBackend
    notebook: NotebookService
    runner: KernelRunner
    runs: RunService
    checkpoints: CheckpointService
    paper_envs: PaperEnvService
    files: FileService
    _threads: set[threading.Thread] = field(default_factory=set)

    # ---- lifecycle
    def startup(self) -> dict[str, Any]:
        """Clean up what a crashed previous process left behind (orphaned kernels, half-built envs)."""
        killed = self.backend.cleanup_orphans() if hasattr(self.backend, "cleanup_orphans") else []
        removed = self.envs.cleanup_temp()
        return {"orphaned_kernels_killed": killed, "temp_envs_removed": removed}

    def shutdown(self, join_timeout: float = 10.0) -> None:
        for t in list(self._threads):
            for env in {e.id for e in self.paper_envs.list()}:
                self.runner.interrupt(env)
            t.join(join_timeout)
        self.runner.shutdown()
        if hasattr(self.backend, "shutdown_all"):
            self.backend.shutdown_all()

    def run_in_background(self, fn: Callable[[], Any]) -> threading.Thread:
        """Execute a long operation (a run) without blocking the caller; errors are published, not lost."""
        def target():
            try:
                fn()
            except Exception:  # RunService already finalised the run; log for operators
                import logging
                logging.getLogger(__name__).exception("background operation failed")
            finally:
                self._threads.discard(threading.current_thread())
        t = threading.Thread(target=target, daemon=True)
        self._threads.add(t)
        t.start()
        return t


def build_app(settings: Settings | None = None, *, db_url: str | None = None, hook: PreExecuteHook | None = None,
              secrets: SecretsProvider | None = None, uow_factory: UnitOfWorkFactory | None = None,
              backend: ComputeBackend | None = None, hf_endpoint: str = "https://huggingface.co",
              network_check: Callable[[Any], bool] | None = None) -> App:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    uow = uow_factory or sqlite_uow_factory(db_url or f"sqlite:///{settings.data_dir / 'paper_env.db'}")
    secrets = secrets or EnvVarSecretsProvider({"hf_token": "HF_TOKEN", "url_token": "PAPER_ENV_URL_TOKEN",
                                                "kaggle_key": "KAGGLE_KEY"})
    scrubber = Scrubber(secrets.known_values())
    bus = EventBus(uow, scrubber)
    gate = ActionGate(hook or AllowAllHook(), uow, bus)
    envs = EnvManager(settings, uow, bus, gate)
    datasets = DatasetStore(settings, uow, bus, gate, secrets, hf_endpoint=hf_endpoint, network_check=network_check)
    backend = backend or LocalBackend(settings, scrubber)
    notebook = NotebookService(uow, bus)
    runner = KernelRunner(settings, uow, bus, gate, backend, notebook, datasets=datasets)
    runs = RunService(settings, uow, bus, gate, runner, notebook, envs, backend, datasets=datasets)
    checkpoints = CheckpointService(settings, uow, bus, backend, runner, notebook, runs._kernel_opts)
    runs.checkpoints = checkpoints
    return App(settings, uow, scrubber, secrets, bus, gate, envs, datasets, backend, notebook, runner, runs,
               checkpoints, PaperEnvService(uow, envs), FileService(settings))
