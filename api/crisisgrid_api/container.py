"""Composition root: wires repositories, gateway and the orchestrator. The only place that knows every concrete class."""
from __future__ import annotations

from typing import Any

from dataclasses import dataclass

from sqlalchemy import Engine

from .agents.artifact_finder import GitHubFetcher, RepoFetcher
from .auth.deps import AuthDeps, build_deps
from .auth.service import AuthService, resolve_secret
from .db.auth_repo import SqlAuthRepo
from .config import Settings
from .db.base import make_session_factory
from .db.event_store import SqlEventStore
from .db.job_repo import SqlJobRepo
from .db.llm_audit import SqlAuditLog
from .db.pipeline_repos import SqlGateRepo, SqlPaperRepo, SqlRunRepo
from .gateway.gateway import AgentProfile, Gateway, Provider
from .gateway.litellm_client import LiteLLMClient
from .ingest.embed import Embedder, HashEmbedder
from .jobs.service import JobService
from .orchestration.gates import GateManager
from .orchestration.pipeline import Orchestrator, PipelineDeps
from .repos.interfaces import EventStore, GateRepo, PaperRepo, RunRepo


@dataclass
class AppContainer:
    papers: PaperRepo
    runs: RunRepo
    gate_repo: GateRepo
    events: EventStore
    gates: GateManager
    orchestrator: Orchestrator | None  # None when no LLM is configured: reading works, starting runs is refused
    jobs: JobService
    auth: AuthService
    auth_deps: AuthDeps
    audit_log: SqlAuditLog | None = None  # the model-call log; erased per paper on delete
    session_factory: Any = None


def gateway_from_settings(settings: Settings, audit: SqlAuditLog) -> Gateway | None:
    models = [m.strip() for m in settings.llm_chain.split(",") if m.strip()]
    if not models:
        return None
    client = LiteLLMClient(api_key=settings.llm_api_key or None, api_base=settings.llm_api_base or None)
    long_out = AgentProfile(max_tokens=settings.llm_max_tokens)  # whole-paper extraction and code writing need room
    return Gateway([Provider(name=m, model=m, client=client) for m in models], audit=audit,
                   profiles={"default": long_out, "paper_parser": long_out, "notebook_gen": AgentProfile(max_tokens=max(settings.llm_max_tokens, 6144))})


def build_container(engine: Engine, settings: Settings, *, gateway: Gateway | None = None, embedder: Embedder | None = None,
                    fetcher: RepoFetcher | None = None) -> AppContainer:
    factory = make_session_factory(engine)
    events, papers, runs, gate_repo = SqlEventStore(factory), SqlPaperRepo(factory), SqlRunRepo(factory), SqlGateRepo(factory)
    gates = GateManager(gate_repo, events)
    jobs = JobService(SqlJobRepo(factory), events)
    audit_log = SqlAuditLog(factory)
    gateway = gateway or gateway_from_settings(settings, audit_log)
    orch = None
    if gateway is not None:
        orch = Orchestrator(PipelineDeps(
            gateway=gateway, embedder=embedder or HashEmbedder(), fetcher=fetcher or GitHubFetcher(token=settings.github_token or None),
            papers=papers, runs=runs, gates=gates, events=events, jobs=jobs,
            generate_notebooks=settings.generate_notebooks))
        jobs.on_finished = lambda job: orch.on_job_finished(job.run_id)  # reaper / cancel path
    auth = AuthService(SqlAuthRepo(factory), settings, resolve_secret(settings) if settings.auth_enabled else "auth-disabled-placeholder")
    return AppContainer(papers=papers, runs=runs, gate_repo=gate_repo, events=events, gates=gates, orchestrator=orch, jobs=jobs,
                        auth=auth, auth_deps=build_deps(auth, settings), audit_log=audit_log,
                        session_factory=factory)
