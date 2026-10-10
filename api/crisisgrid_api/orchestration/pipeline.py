"""Pipeline orchestrator (system_design 3.1), Ingest -> ... -> Report -> Final gate.

A persisted state machine: one method per node, state saved to the database after every node, gates are database rows.
A server restart mid-wait therefore loses nothing: `respond()` rebuilds everything from the database.

Nodes after Route that need compute (Environment, Execute, Diagnose, Verify, Attribute) arrive with P6-P8; until then
`reconstruct` records, honestly, that no execution took place and the run ends with a Tier-1 report.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from crisisgrid_core.harness import EscalationRequired, HarnessConfig, HarnessGuard
from crisisgrid_core.models import utcnow
from crisisgrid_core.routing import select_route

from ..agents.artifact_finder import RepoFetcher, find_artifacts
from ..agents.gap_auditor import GapAuditorAgent
from ..agents.paper_parser import PaperParserAgent
from ..gateway.gateway import Gateway
from ..gateway.types import AllProvidersFailed
from ..ingest.embed import Embedder
from ..ingest.pdf import IngestedPaper, PdfError, ingest_pdf
from ..repos.interfaces import EventStore, PaperRepo, RunEvent, RunRepo
from .gates import GateError, GateManager
from .models import Gate, GateKind, RunState
from .report import build_report

log = logging.getLogger("crisisgrid.pipeline")


@dataclass
class GateRequest:
    kind: GateKind
    context: dict[str, Any] = field(default_factory=dict)


Step = str | GateRequest

# where "skip this stage" leads when a human skips a stuck stage
SKIP_NEXT = {"ingest": "report", "understand": "report", "audit": "report", "route": "report"}


@dataclass
class PipelineDeps:
    gateway: Gateway
    embedder: Embedder
    fetcher: RepoFetcher
    papers: PaperRepo
    runs: RunRepo
    gates: GateManager
    events: EventStore
    guard_config: HarnessConfig = field(default_factory=HarnessConfig)


class Orchestrator:
    def __init__(self, deps: PipelineDeps) -> None:
        self.d = deps
        self._guards: dict[str, HarnessGuard] = {}  # budgets are per process; a restart starts fresh budgets

    # ---- plumbing ------------------------------------------------------------------------------------------
    def _guard(self, run_id: str) -> HarnessGuard:
        return self._guards.setdefault(run_id, HarnessGuard(self.d.guard_config))

    def _emit(self, run_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.d.events.append(RunEvent(run_id=run_id, module="pipeline", kind=kind, payload=payload or {}))

    def _load(self, run_id: str) -> RunState:
        s = self.d.runs.get(run_id)
        if s is None:
            raise KeyError(run_id)
        return s  # type: ignore[no-any-return]

    def _paper(self, state: RunState) -> IngestedPaper:
        pdf = self.d.papers.get_pdf(state.paper_id)
        if pdf is None:
            raise PdfError(f"paper {state.paper_id} not found")
        return ingest_pdf(pdf)

    # ---- lifecycle -----------------------------------------------------------------------------------------
    def start(self, paper_id: str) -> RunState:
        if self.d.papers.get(paper_id) is None:
            raise KeyError(paper_id)
        state = RunState(paper_id=paper_id)
        self.d.runs.save(state)
        self._emit(state.run_id, "run_started", {"paper_id": paper_id})
        return state

    def advance(self, run_id: str) -> RunState:
        state = self._load(run_id)
        guard = self._guard(run_id)
        while state.status == "running":
            node = state.node
            if node == "end":
                state.status = "completed"
                self._emit(run_id, "run_finished", {"status": "completed"})
            else:
                self._emit(run_id, "stage_started", {"node": node})
                try:
                    step = getattr(self, f"_node_{node}")(state, guard)
                except PdfError as e:
                    self._fail(state, f"unreadable paper: {e}")
                except (EscalationRequired, AllProvidersFailed) as e:
                    reason = e.reason if isinstance(e, EscalationRequired) else str(e)
                    self._open_gate(state, GateRequest("stuck", {"node": node, "reason": reason}), error=reason)
                except Exception as e:  # noqa: BLE001  (a bug must end the run visibly, never hang it)
                    log.exception("node failed", extra={"run_id": run_id, "node": node})
                    self._fail(state, f"{node}: {type(e).__name__}: {e}")
                else:
                    self._emit(run_id, "stage_done", {"node": node})
                    if isinstance(step, GateRequest):
                        self._open_gate(state, step)
                    else:
                        state.node = step
            state.updated_at = utcnow()
            self.d.runs.save(state)
        return state

    def _fail(self, state: RunState, error: str) -> None:
        state.status, state.error = "failed", error
        self._emit(state.run_id, "run_failed", {"error": error, "node": state.node})

    def _open_gate(self, state: RunState, req: GateRequest, error: str | None = None) -> None:
        exclude: list[str] = []
        if req.kind == "stuck":
            exclude.append("edit_code")  # needs execution stages (P6+)
            # a human retry of the very same failure is a loop: the guard says so, and we stop offering it
            if error is not None and not self._guard(state.run_id).record_attempt(state.node, "retry", error).ok:
                exclude.append("retry_with_hint")
                req.context["retry_blocked"] = "the same failure has repeated; change something or skip/abort"
            state.error = error
        gate = self.d.gates.open(state.run_id, req.kind, req.context, exclude=exclude)
        state.status, state.waiting_gate_id = "waiting_gate", gate.id

    def respond(self, gate_id: str, action: str, payload: dict[str, Any] | None = None) -> RunState:
        """Answer a gate and move the run to its next node (status `running`). The caller then calls `advance()`, which may
        be slow (LLM calls), so the HTTP layer does that in the background. Raises GateError for an invalid answer."""
        gate = self.d.gates.gates.get(gate_id)
        if gate is None:
            raise KeyError(gate_id)
        state = self._load(gate.run_id)
        if state.waiting_gate_id != gate_id and gate.status == "open":
            raise GateError("gate is not the one this run is waiting on")
        payload = payload or {}
        if gate.kind == "stuck" and action == "edit_code":
            raise GateError("edit_code is only available during execution stages (not built yet)")
        if gate.kind == "action":
            raise GateError("action gates are created by the Action hook (not built yet)")
        resolved = self.d.gates.respond(gate_id, action, payload)
        self._apply(state, resolved, payload)
        state.waiting_gate_id = None
        if state.status == "waiting_gate":
            state.status = "running"
        state.updated_at = utcnow()
        self.d.runs.save(state)
        return state

    def _apply(self, state: RunState, gate: Gate, payload: dict[str, Any]) -> None:
        a = gate.action
        if gate.kind == "data":
            state.data_resolution = {"upload": "upload", "substitute": "substitute", "stop_tier1": "stopped"}[a or ""]  # type: ignore[assignment]
            state.node = "report" if a == "stop_tier1" else "reconstruct"
        elif gate.kind == "gap":
            if a == "reject":
                state.caveats.append("The user rejected the proposed gap list/assumptions; nothing was reconstructed.")
                state.node = "report"
            else:
                state.assumptions_approved = True
                if a == "edit" and payload.get("note"):
                    state.caveats.append(f"Assumptions edited by the user: {payload['note']}")
                state.node = "reconstruct"
        elif gate.kind == "stuck":
            node = state.node
            if a == "retry_with_hint":
                state.hints[node] = str(payload.get("hint", ""))
                state.error = None
            elif a == "skip_stage":
                state.caveats.append(f"Stage '{node}' was skipped by the user after a failure ({state.error}); results are incomplete.")
                state.error = None
                state.node = SKIP_NEXT.get(node, "report")
            elif a == "abort":
                self._fail(state, "aborted by the user")
        elif gate.kind == "final":
            if a == "accept":
                state.node = "end"
            elif a == "request_rerun":
                state.parsed = state.inventory = state.route = None
                state.gaps, state.assumptions_approved, state.data_resolution = [], False, None
                state.node = "understand"
            elif a == "edit_caveats":
                extra = payload.get("caveats") or []
                state.caveats.extend(str(c) for c in (extra if isinstance(extra, list) else [extra]))
                state.node = "report"

    # ---- nodes ---------------------------------------------------------------------------------------------
    def _node_ingest(self, state: RunState, guard: HarnessGuard) -> Step:
        paper = self._paper(state)
        state.needs_ocr = paper.needs_ocr
        return "report" if paper.needs_ocr else "understand"

    def _node_understand(self, state: RunState, guard: HarnessGuard) -> Step:
        paper = self._paper(state)
        state.parsed = PaperParserAgent(self.d.gateway, guard, self.d.embedder).parse(
            paper.pages, run_id=state.run_id, hint=state.hints.get("understand"))
        return "audit"

    def _node_audit(self, state: RunState, guard: HarnessGuard) -> Step:
        if state.parsed is None:
            raise EscalationRequired("audit has no parsed paper to work on")
        paper = self._paper(state)
        pub: date | None = state.parsed.published or paper.metadata_date
        state.inventory = find_artifacts(paper.text, state.parsed, self.d.fetcher, paper_date=pub)
        state.gaps = GapAuditorAgent(self.d.gateway, guard).audit(
            state.parsed, state.inventory, paper.text, run_id=state.run_id, hint=state.hints.get("audit"))
        return "route"

    def _node_route(self, state: RunState, guard: HarnessGuard) -> Step:
        assert state.parsed is not None and state.inventory is not None
        state.route = select_route(state.parsed.claims, state.gaps, state.inventory)
        self._emit(state.run_id, "route_selected", state.route.model_dump(mode="json"))
        return {1: "data_gate", 3: "gap_gate", 2: "reconstruct"}[state.route.case]

    def _node_data_gate(self, state: RunState, guard: HarnessGuard) -> Step:
        assert state.route is not None and state.inventory is not None
        return GateRequest("data", {
            "reasons": state.route.reasons,
            "datasets": [d.model_dump(mode="json") for d in state.inventory.datasets],
            "blocking_gaps": [g.model_dump(mode="json") for g in state.gaps if g.severity == "blocking"],
        })

    def _node_gap_gate(self, state: RunState, guard: HarnessGuard) -> Step:
        assert state.inventory is not None
        return GateRequest("gap", {
            "missing_components": state.inventory.missing_components,
            "gaps": [g.model_dump(mode="json") for g in state.gaps if g.severity in ("blocking", "major")],
            "assumptions": [],  # proposed by the Code rebuilder (Tier 3, not built yet)
        })

    def _node_reconstruct(self, state: RunState, guard: HarnessGuard) -> Step:
        msg = "Environment build, execution and claim verification are not available in this build; no experiment was run."
        if msg not in state.caveats:
            state.caveats.append(msg)
        return "report"

    def _node_report(self, state: RunState, guard: HarnessGuard) -> Step:
        state.report = build_report(state)
        return "final_gate"

    def _node_final_gate(self, state: RunState, guard: HarnessGuard) -> Step:
        return GateRequest("final", {"report": state.report, "caveats": state.caveats})
