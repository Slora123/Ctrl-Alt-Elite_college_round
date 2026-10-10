"""Composition root: wires repositories, gateway and the orchestrator. The only place that knows every concrete class."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from .agents.artifact_finder import GitHubFetcher, RepoFetcher
from .config import Settings
from .db.base import make_session_factory
from .db.event_store import SqlEventStore
from .db.llm_audit import SqlAuditLog
from .db.pipeline_repos import SqlGateRepo, SqlPaperRepo, SqlRunRepo
from .gateway.gateway import Gateway, Provider
from .gateway.litellm_client import LiteLLMClient
from .ingest.embed import Embedder, HashEmbedder
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


def gateway_from_settings(settings: Settings, audit: SqlAuditLog) -> Gateway | None:
    models = [m.strip() for m in settings.llm_chain.split(",") if m.strip()]
    if not models:
        return None
    client = LiteLLMClient(api_key=settings.llm_api_key or None)
    return Gateway([Provider(name=m, model=m, client=client) for m in models], audit=audit)


def build_container(engine: Engine, settings: Settings, *, gateway: Gateway | None = None, embedder: Embedder | None = None,
                    fetcher: RepoFetcher | None = None) -> AppContainer:
    factory = make_session_factory(engine)
    events, papers, runs, gate_repo = SqlEventStore(factory), SqlPaperRepo(factory), SqlRunRepo(factory), SqlGateRepo(factory)
    gates = GateManager(gate_repo, events)
    gateway = gateway or gateway_from_settings(settings, SqlAuditLog(factory))
    orch = None
    if gateway is not None:
        orch = Orchestrator(PipelineDeps(
            gateway=gateway, embedder=embedder or HashEmbedder(), fetcher=fetcher or GitHubFetcher(token=settings.github_token or None),
            papers=papers, runs=runs, gates=gates, events=events))
    return AppContainer(papers=papers, runs=runs, gate_repo=gate_repo, events=events, gates=gates, orchestrator=orch)
