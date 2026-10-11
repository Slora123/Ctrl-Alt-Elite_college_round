"""Pipeline orchestrator (system_design 3.1), Ingest -> ... -> Report -> Final gate.

A persisted state machine: one method per node, state saved to the database after every node, gates are database rows.
A server restart mid-wait therefore loses nothing: `respond()` rebuilds everything from the database.

Nodes after Route that need compute (Environment, Execute, Diagnose, Verify, Attribute) arrive with P6-P8; until then
`reconstruct` records, honestly, that no execution took place and the run ends with a Tier-1 report.
"""
from __future__ import annotations

import difflib
import logging
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from crisisgrid_core.action_hook import Action, ActionHook
from crisisgrid_core.harness import EscalationRequired, HarnessConfig, HarnessGuard
from crisisgrid_core.jobs import JobSpec
from crisisgrid_core.models import RunResult, utcnow
from crisisgrid_core.routing import select_route
from crisisgrid_core.verifier import verify_claim

from ..agents.artifact_finder import RepoFetcher, find_artifacts
from ..agents.gap_auditor import GapAuditorAgent
from ..agents.paper_parser import PaperParserAgent
from ..agents.notebook_gen import NotebookGeneratorAgent
from ..agents.report_writer import ReportWriterAgent
from ..agents.run_diagnoser import RunDiagnoserAgent
from ..gateway.gateway import Gateway
from ..gateway.types import AllProvidersFailed
from ..ingest.embed import Embedder
from ..ingest.pdf import IngestedPaper, PdfError, ingest_pdf
from ..jobs.service import JobService
from ..repos.interfaces import EventStore, PaperRepo, RunEvent, RunRepo
from .gates import GateError, GateManager
from .models import AgentEdit, Gate, GateKind, RunState
from .report import build_report

log = logging.getLogger("crisisgrid.pipeline")


@dataclass
class GateRequest:
    kind: GateKind
    context: dict[str, Any] = field(default_factory=dict)


Step = str | GateRequest

# where "skip this stage" leads when a human skips a stuck stage
SKIP_NEXT = {"ingest": "report", "understand": "report", "audit": "report", "route": "report", "generate": "reconstruct"}


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
    jobs: JobService | None = None  # None: no execution (Tier 1 only)
    narrative: bool = False  # ask the Report writer for a short summary (one extra model call)
    repair: bool = False  # diagnose failed clean runs and propose fixes; off: a failed run goes straight to a human
    generate_notebooks: bool = False  # no notebook attached: let the generator write one (shown at a 'notebook' gate first)
    # bridge to the Paper Environment (set by the app when paper_env is mounted): put generated cells into an editable
    # notebook, and read the (possibly edited) cells back when the human approves
    notebook_sink: Callable[[str, list[dict[str, str]], list[str]], str | None] | None = None
    notebook_source: Callable[[str], list[dict[str, str]] | None] | None = None


class Orchestrator:
    def __init__(self, deps: PipelineDeps) -> None:
        self.d = deps
        self._guards: dict[str, HarnessGuard] = {}  # budgets are per process; a restart starts fresh budgets
        self._locks: dict[str, threading.RLock] = {}

    # ---- plumbing ------------------------------------------------------------------------------------------
    def _guard(self, run_id: str) -> HarnessGuard:
        return self._guards.setdefault(run_id, HarnessGuard(self.d.guard_config))

    def _lock(self, run_id: str) -> threading.RLock:
        return self._locks.setdefault(run_id, threading.RLock())

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
    def start(self, paper_id: str, *, notebook: list[dict[str, str]] | None = None, seeds: list[int] | None = None) -> RunState:
        if self.d.papers.get(paper_id) is None:
            raise KeyError(paper_id)
        state = RunState(paper_id=paper_id, notebook=notebook)
        if seeds:
            state.seeds = seeds
        self.d.runs.save(state)
        self._emit(state.run_id, "run_started", {"paper_id": paper_id})
        return state

    def advance(self, run_id: str) -> RunState:
        with self._lock(run_id):  # two finishing jobs (or a job and a human) must not advance the same run twice
            return self._advance(run_id)

    def _advance(self, run_id: str) -> RunState:
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
        if gate.kind == "action" and action == "edit" and not (isinstance(payload.get("source"), str) and payload["source"].strip()):
            raise GateError("answer 'edit' needs payload.source: the replacement source of the cell")  # validated BEFORE resolving
        if gate.kind == "notebook" and action == "edit":
            cells = payload.get("cells")
            if not (isinstance(cells, list) and cells and all(isinstance(c, dict) and isinstance(c.get("source"), str) for c in cells)):
                raise GateError("answer 'edit' needs payload.cells: a non-empty list of {\"source\": str}")
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
            state.node = "report" if a == "stop_tier1" else self._after_gates(state)
        elif gate.kind == "gap":
            if a == "reject":
                state.caveats.append("The user rejected the proposed gap list/assumptions; nothing was reconstructed.")
                state.node = "report"
            else:
                state.assumptions_approved = True
                if a == "edit" and payload.get("note"):
                    state.caveats.append(f"Assumptions edited by the user: {payload['note']}")
                state.node = self._after_gates(state)
        elif gate.kind == "stuck":
            node = state.node
            if a == "retry_with_hint":
                state.hints[node] = str(payload.get("hint", ""))
                state.error = None
                if node in ("execute", "diagnose"):
                    state.job_ids, state.node = [], "execute"  # a retry enqueues fresh jobs
            elif a == "skip_stage":
                state.caveats.append(f"Stage '{node}' was skipped by the user after a failure ({state.error}); results are incomplete.")
                state.error = None
                state.node = SKIP_NEXT.get(node, "report")
            elif a == "abort":
                self._fail(state, "aborted by the user")
        elif gate.kind == "notebook":
            if a == "reject":
                state.caveats.append("The generated notebook was rejected by the reviewer; no experiment was run.")
                state.notebook, state.node = None, "report"
            else:
                if a == "edit":
                    state.notebook = [{"kind": "code", "source": str(c["source"])} for c in payload["cells"]]
                elif state.notebook_env_id and self.d.notebook_source is not None:
                    # run exactly what the reviewer sees in the Notebook page (they may have edited it there)
                    current = self.d.notebook_source(state.notebook_env_id)
                    if current:
                        state.notebook = current
                state.job_ids, state.node = [], "execute"
        elif gate.kind == "action":
            pending = state.pending_edit
            if pending is None:
                self._fail(state, "action gate answered but no edit is pending")
            elif a == "reject":
                state.pending_edit = None
                state.caveats.append(f"A proposed repair of cell {pending.cell_index} was rejected; the run ended without results.")
                state.node = "report"
            else:
                if a == "edit":
                    pending = self._edit(state, pending.cell_index, str(payload["source"]), "human",
                                         "rewritten by a reviewer instead of the proposed fix")
                self._apply_edit(state, pending.model_copy(update={"approved_by": "human"}))
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
        return {1: "data_gate", 3: "gap_gate", 2: self._after_gates(state)}[state.route.case]

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

    def _after_gates(self, state: RunState) -> str:
        """Run the notebook we were given; otherwise write one (if enabled) for a human to review; otherwise be honest that
        nothing executes."""
        if state.notebook and self.d.jobs is not None:
            return "execute"
        if self.d.generate_notebooks and self.d.jobs is not None and state.parsed is not None:
            return "generate"
        return "reconstruct"

    def _node_generate(self, state: RunState, guard: HarnessGuard) -> Step:
        """Write a runnable (usually scaled-down) notebook from the extracted setup. It never runs before a human approves it."""
        assert state.parsed is not None
        nb, notes = NotebookGeneratorAgent(self.d.gateway, guard).generate(
            state.parsed, state.gaps, run_id=state.run_id, hint=state.hints.get("generate"))
        state.notebook = [{"kind": "code", "source": c.source} for c in nb.cells]
        state.generated, state.requirements, state.scaled_down = True, nb.requirements, nb.scaled_down
        state.assumptions = [*nb.assumptions, *([nb.dataset_note] if nb.dataset_note else []), *notes]
        state.job_ids = []
        if self.d.notebook_sink is not None:
            try:
                state.notebook_env_id = self.d.notebook_sink(state.paper_id, state.notebook, state.requirements)
            except Exception as e:  # noqa: BLE001  (the review still works from the gate; only the editable copy is missing)
                log.warning("could not copy the generated notebook into a paper environment: %s", e)
        self._emit(state.run_id, "notebook_generated", {"cells": len(state.notebook), "requirements": state.requirements,
                                                         "scaled_down": state.scaled_down, "env_id": state.notebook_env_id})
        return GateRequest("notebook", {
            "cells": [{"purpose": c.purpose, "source": c.source} for c in nb.cells],
            "requirements": state.requirements, "assumptions": state.assumptions, "scaled_down": state.scaled_down,
            "env_id": state.notebook_env_id,
            "note": "Written by the system from the paper's stated setup, not the authors' code. Results will be directional only.",
        })

    def _node_execute(self, state: RunState, guard: HarnessGuard) -> Step:
        """Clean runs (fresh process, fixed seed, top to bottom) on workers; one job per seed. Interactive runs never get here."""
        jobs = self.d.jobs
        assert jobs is not None and state.notebook
        if not state.job_ids:
            specs = [JobSpec(run_id=state.run_id, graph_id=f"paper:{state.paper_id}", mode="clean_notebook", seed=sd,
                             notebook_version=1, payload={"cells": state.notebook, "requirements": state.requirements}) for sd in state.seeds]
            why = jobs.unschedulable_reasons(specs[0])
            if why and why != ["no worker is registered"]:
                # workers exist but none can run this job: waiting would never help, so a human decides
                raise EscalationRequired("no worker can run this job: " + "; ".join(why))
            state.job_ids = [jobs.enqueue(sp).id for sp in specs]
            self._emit(state.run_id, "jobs_queued", {"job_ids": state.job_ids, "seeds": state.seeds})
            if why:  # nobody registered yet: queue anyway, a worker may connect later (outbound-only workers poll)
                self._emit(state.run_id, "awaiting_worker", {"detail": "no worker is registered yet; jobs stay queued"})
            state.status = "waiting_jobs"
            return "execute"
        found = [jobs.repo.get_job(j) for j in state.job_ids]
        done = [j for j in found if j is not None]
        if len(done) != len(state.job_ids) or any(j.status in ("queued", "assigned", "running") for j in done):
            state.status = "waiting_jobs"
            return "execute"
        status_map = {"succeeded": "ok", "failed": "failed", "timeout": "timeout", "infra_failed": "infra_failed"}
        results: list[RunResult] = []
        for j in done:
            if j.status not in status_map:
                continue  # cancelled
            res = j.result or {}
            st = status_map[j.status]
            if st == "ok" and state.scaled_down:
                st = "scaled_down"  # the verifier caps scaled-down evidence at 'partial'
            results.append(RunResult(
                run_id=j.id, graph_id=j.spec.graph_id, seed=j.spec.seed, status=st,  # type: ignore[arg-type]
                scale_note="; ".join(state.assumptions)[:500] if st == "scaled_down" else None,
                mode="clean", verification_eligible=True, metrics=res.get("metrics", {}), log_ref=f"events:{state.run_id}:{j.id}",
                lockfile_digest=res.get("lockfile_digest") or None, hardware_info=res.get("hardware_info", {}),
                notebook_version=j.spec.notebook_version))
            w = jobs.repo.get_worker(j.worker_id) if j.worker_id else None
            if w is not None and w.capabilities.isolation == "remote_vm":
                msg = ("Executed on a remote cloud VM (Google Colab): the notebook code and its results left this machine; "
                       "the VM is Google's, not a sandbox we control.")
                if msg not in state.caveats:
                    state.caveats.append(msg)
            elif w is not None and w.capabilities.isolation != "docker":
                msg = "Executed on a lower-isolation worker (no Docker): untrusted code ran in a restricted subprocess."
                if msg not in state.caveats:
                    state.caveats.append(msg)
        state.run_results, state.execution = results, "clean_runs"
        if not any(r.status in ("ok", "scaled_down") for r in results):
            if self.d.repair and any(j.status == "failed" and (j.result or {}).get("error") for j in done):
                return "diagnose"
            errs = "; ".join(sorted({j.error or j.status for j in done}))[:300]
            raise EscalationRequired(f"no clean run produced a result ({errs})")
        return "verify"

    def _node_verify(self, state: RunState, guard: HarnessGuard) -> Step:
        assert state.parsed is not None
        mismatch = None
        if state.data_resolution == "substitute":
            mismatch = "substitute data was used: this is not a reproduction of the paper's setting"
        if (state.route and state.route.case == 3) or state.generated:
            msg = ("The executed notebook was written by the system, not the authors: results are directional only."
                   if state.generated else "The executed notebook is not the authors' code (Tier 3): results are directional only.")
            if msg not in state.caveats:
                state.caveats.append(msg)
        agent_edits = [e for e in state.edits if e.author == "agent"]
        if agent_edits:
            state.caveats.append(f"{len(agent_edits)} agent-authored edit(s) were applied to get the notebook to run; see the deviations.")
        state.verdicts = [verify_claim(c, state.run_results, metric_mismatch=mismatch) for c in state.parsed.claims]
        for v in state.verdicts:
            self._emit(state.run_id, "verdict_ready", v.model_dump(mode="json"))
        return "report"

    def _edit(self, state: RunState, i: int, new: str, author: str, reason: str, risk: str = "experiment_changing") -> AgentEdit:
        assert state.notebook is not None
        old = state.notebook[i]["source"]
        diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"cell {i} (before)", f"cell {i} (after)", lineterm=""))
        return AgentEdit(cell_index=i, old=old, new=new, author=author, reason=reason, diff=diff, risk_class=risk)  # type: ignore[arg-type]

    def _apply_edit(self, state: RunState, edit: AgentEdit) -> None:
        assert state.notebook is not None
        state.notebook[edit.cell_index] = {**state.notebook[edit.cell_index], "source": edit.new}
        state.edits.append(edit)
        state.pending_edit, state.job_ids, state.node = None, [], "execute"
        self._emit(state.run_id, "edit_applied", {"edit_id": edit.id, "cell_index": edit.cell_index, "author": edit.author,
                                                   "approved_by": edit.approved_by, "risk_class": edit.risk_class, "diff": edit.diff})

    def _node_diagnose(self, state: RunState, guard: HarnessGuard) -> Step:
        """A clean run failed in user code. Diagnose, then act only as far as the Action hook allows."""
        jobs = self.d.jobs
        assert jobs is not None and state.notebook
        candidates = [jobs.repo.get_job(i) for i in state.job_ids]
        failed = next((j for j in candidates if j and j.status == "failed" and (j.result or {}).get("error")), None)
        assert failed is not None and failed.result is not None
        err = failed.result["error"]
        step = guard.record_attempt("repair", "diagnose", f"{err.get('type')}: {err.get('message')}")
        if not step.ok:  # the same failure after a repair means the repair did not work
            raise EscalationRequired(f"repair gave up: {step.reason}")
        diag = RunDiagnoserAgent(self.d.gateway, guard).diagnose(err, state.notebook, run_id=state.run_id, hint=state.hints.get("diagnose"))
        state.diagnoses.append(diag.model_dump(mode="json"))
        self._emit(state.run_id, "diagnosis", {"category": diag.category, "explanation": diag.explanation, "has_fix": diag.fix is not None})
        why = f"diagnosis [{diag.category}]: {diag.explanation}"
        if diag.category == "infra":
            state.job_ids, state.status = [], "running"
            return "execute"  # retried, not patched
        if diag.install:
            why += f" (needs packages {', '.join(diag.install)}; install them on the worker, then retry)"
        if diag.needs_human or diag.fix is None:
            raise EscalationRequired(why)
        i, new = diag.fix.cell_index, diag.fix.new_source
        old = state.notebook[i]["source"]
        action = Action(kind="edit", author="agent", target=f"cell {i}", payload={"old": old, "new": new})
        decision = ActionHook(guard).check(action, "repair")
        edit = self._edit(state, i, new, "agent", diag.explanation, decision.risk_class)
        if decision.decision == "allow":
            self._apply_edit(state, edit.model_copy(update={"approved_by": "policy"}))
            return "execute"
        if decision.decision == "needs_approval":
            state.pending_edit = edit
            return GateRequest("action", {"cell_index": i, "diagnosis": diag.model_dump(mode="json"), "diff": edit.diff,
                                          "risk_class": decision.risk_class, "reason": decision.reason, "error": err,
                                          "proposed_source": new})  # the UI starts the reviewer's "edit" answer from this
        raise EscalationRequired(f"the Action hook denied the proposed repair: {decision.reason}")

    def on_job_finished(self, run_id: str) -> None:
        """Called when any job of a run ends (HTTP completion, reaper, cancel). Continues the run once ALL its jobs are done."""
        with self._lock(run_id):
            state = self.d.runs.get(run_id)
            if state is None or state.status != "waiting_jobs":
                return
            jobs = self.d.jobs
            assert jobs is not None
            for i in state.job_ids:
                j = jobs.repo.get_job(i)
                if j is None or j.status in ("queued", "assigned", "running"):
                    return
            state.status = "running"
            self.d.runs.save(state)
            self._advance(run_id)

    def _node_reconstruct(self, state: RunState, guard: HarnessGuard) -> Step:
        msg = ("No experiment was run: no notebook was provided. "
               "Attach a .ipynb that reports its metrics to run clean experiments on a worker.")
        if msg not in state.caveats:
            state.caveats.append(msg)
        return "report"

    def _node_report(self, state: RunState, guard: HarnessGuard) -> Step:
        summary = None
        if self.d.narrative and state.verdicts and state.parsed is not None:
            try:
                writer = ReportWriterAgent(self.d.gateway, guard)
                summary = writer.write(state.parsed.claims, state.verdicts, state.gaps, run_id=state.run_id)
            except (EscalationRequired, AllProvidersFailed) as e:
                # the summary is optional; the deterministic report below already states every number. Say it is missing.
                note = f"The narrative summary was omitted: the writer could not produce one consistent with the evidence ({e})."
                if note not in state.caveats:
                    state.caveats.append(note)
        state.report = build_report(state, summary=summary)
        return "final_gate"

    def _node_final_gate(self, state: RunState, guard: HarnessGuard) -> Step:
        return GateRequest("final", {"report": state.report, "caveats": state.caveats})
