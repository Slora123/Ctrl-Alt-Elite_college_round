"""Composition root: the one place that knows which concrete implementations are used."""
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .auth import Authenticator, StaticTokenAuthenticator, Principal
from .backends import ComputeBackend, LocalBackend
from .backends.registry import BackendRegistry, default_registry
from .config import Settings
from .datasets import DatasetStore
from .env import EnvManager
from .events import EventBus
from .hooks import ActionGate, AllowAllHook, PreExecuteHook
from .kernel.runner import KernelRunner
from .repos import UnitOfWorkFactory
from .repos.sqlite import sqlite_uow_factory
from .security import EnvVarSecretsProvider, Scrubber, SecretsProvider
from .security.store import ChainedSecretsProvider, SecretsStore
from .services.checkpoints import CheckpointService
from .services.files import FileService
from .services.ipynb import IpynbService
from .services.limits import SessionReaper
from .services.notebook import NotebookService
from .services.paper_envs import PaperEnvService
from .services.runs import RunService
from .services.terminal import TerminalService


@dataclass
class App:
    settings: Settings
    uow: UnitOfWorkFactory
    scrubber: Scrubber
    secrets: SecretsProvider
    secrets_store: SecretsStore
    bus: EventBus
    gate: ActionGate
    envs: EnvManager
    datasets: DatasetStore
    backend: ComputeBackend  # the default (local) backend
    registry: BackendRegistry
    notebook: NotebookService
    runner: KernelRunner
    runs: RunService
    checkpoints: CheckpointService
    paper_envs: PaperEnvService
    files: FileService
    ipynb: IpynbService
    terminal: TerminalService
    reaper: SessionReaper
    authenticator: Authenticator | None = None
    _threads: set[threading.Thread] = field(default_factory=set)

    # ---- lifecycle
    def startup(self) -> dict[str, Any]:
        """Clean up what a crashed previous process left behind (orphaned kernels, half-built envs), start the sweeper."""
        killed: list[str] = []
        for b in self.registry.all_backends():
            if hasattr(b, "cleanup_orphans"):
                killed += b.cleanup_orphans()
        removed = self.envs.cleanup_temp()
        self.reaper.start()
        return {"orphaned_kernels_killed": killed, "temp_envs_removed": removed}

    def shutdown(self, join_timeout: float = 10.0) -> None:
        self.reaper.stop()
        self.terminal.close_all()
        for t in list(self._threads):
            for env in {e.id for e in self.paper_envs.list()}:
                self.runner.interrupt(env)
            t.join(join_timeout)
        self.runner.shutdown()
        for b in self.registry.all_backends():
            if hasattr(b, "shutdown_all"):
                b.shutdown_all()

    def run_in_background(self, fn: Callable[[], Any]) -> threading.Thread:
        """Execute a long operation (a run) without blocking the caller; errors are logged, not lost. The caller's
        context (including the authenticated principal) is carried into the thread."""
        ctx = contextvars.copy_context()

        def target():
            try:
                ctx.run(fn)
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
              backend: ComputeBackend | None = None, registry: BackendRegistry | None = None,
              hf_endpoint: str = "https://huggingface.co", network_check: Callable[[Any], bool] | None = None,
              authenticator: Authenticator | None = None) -> App:
    import os
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    uow = uow_factory or sqlite_uow_factory(db_url or f"sqlite:///{settings.data_dir / 'paper_env.db'}")
    base_secrets = secrets or EnvVarSecretsProvider({"hf_token": "HF_TOKEN", "url_token": "PAPER_ENV_URL_TOKEN",
                                                     "kaggle_key": "KAGGLE_KEY"})
    store = SecretsStore(settings.data_dir / "secrets")
    secrets_provider = ChainedSecretsProvider(store, base_secrets)
    scrubber = Scrubber(secrets_provider.known_values())
    bus = EventBus(uow, scrubber)
    gate = ActionGate(hook or AllowAllHook(), uow, bus)
    envs = EnvManager(settings, uow, bus, gate)
    datasets = DatasetStore(settings, uow, bus, gate, secrets_provider, hf_endpoint=hf_endpoint, network_check=network_check)
    local = backend or LocalBackend(settings, scrubber)
    registry = registry or default_registry(local)
    notebook = NotebookService(uow, bus)
    runner = KernelRunner(settings, uow, bus, gate, registry, notebook, datasets=datasets)
    runs = RunService(settings, uow, bus, gate, runner, notebook, envs, registry.default, datasets=datasets)
    runs.secrets_store = store
    checkpoints = CheckpointService(settings, uow, bus, registry.default, runner, notebook, runs._kernel_opts)
    runs.checkpoints = checkpoints
    if authenticator is None and os.environ.get("PAPER_ENV_API_TOKEN"):
        authenticator = StaticTokenAuthenticator({os.environ["PAPER_ENV_API_TOKEN"]: Principal("api-token", "api-token", "admin")})
    return App(settings, uow, scrubber, secrets_provider, store, bus, gate, envs, datasets, registry.default, registry,
               notebook, runner, runs, checkpoints, PaperEnvService(uow, envs), FileService(settings),
               IpynbService(uow, bus, notebook, envs), TerminalService(settings, scrubber),
               SessionReaper(uow, runner, settings.reaper_interval_s), authenticator)
