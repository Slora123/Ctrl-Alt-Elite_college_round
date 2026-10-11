"""REST + SSE endpoints (system_design 2). Thin: everything real happens in the orchestrator."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import PurePath
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from crisisgrid_core.models import utcnow

from .auth.models import User
from .container import AppContainer
from .db.graph_repo import SqlGraphRepo
from .graphs import build_graph_router
from .ingest.pdf import PdfError, ingest_pdf
from .orchestration.gates import GateAlreadyResolved, GateError
from .orchestration.models import PaperRecord, RunState
from .orchestration.notebook_io import NotebookError, parse_notebook

MAX_PDF_BYTES = 30 * 1024 * 1024


class StartRun(BaseModel):
    notebook: dict[str, Any] | None = None  # .ipynb JSON (or {"cells": [...]}) to execute as clean runs
    seeds: list[int] | None = Field(default=None, min_length=1, max_length=20)


class GateResponse(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


def _summary(s: RunState) -> dict[str, Any]:
    return {"run_id": s.run_id, "paper_id": s.paper_id, "status": s.status, "node": s.node, "waiting_gate_id": s.waiting_gate_id,
            "error": s.error, "route": s.route.model_dump(mode="json") if s.route else None, "needs_ocr": s.needs_ocr,
            "caveats": s.caveats, "generated": s.generated, "notebook_env_id": s.notebook_env_id, "assumptions": s.assumptions,
            "requirements": s.requirements, "scaled_down": s.scaled_down}


def _paper_title(paper: PaperRecord, runs: list[RunState]) -> str:
    """The title the model extracted from the latest analysis, else the file name."""
    for s in reversed(runs):
        if s.parsed is not None and s.parsed.title.strip():
            return s.parsed.title.strip()
    return PurePath(paper.filename).stem or "Untitled paper"


def _run_brief(s: RunState) -> dict[str, Any]:
    """A run summary plus the counts a library table needs."""
    return {**_summary(s), "claims": len(s.parsed.claims) if s.parsed else 0, "gaps": len(s.gaps),
            "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat()}


VERDICTS = ("reproduced", "partial", "not_reproduced", "inconclusive", "not_executed")


def _sse(kind: str, event_id: str, data: dict[str, Any]) -> str:
    return f"id: {event_id}\nevent: {kind}\ndata: {json.dumps(data, default=str)}\n\n"


def build_router(c: AppContainer) -> APIRouter:
    r = APIRouter()
    cur, writer = c.auth_deps.current_user, c.auth_deps.writer

    def visible(user: User, paper: PaperRecord | None) -> bool:
        return paper is not None and (user.is_admin or paper.owner_id in (None, user.id))

    def paper_for(user: User, paper_id: str) -> PaperRecord:
        p = c.papers.get(paper_id)
        if not visible(user, p):
            raise HTTPException(404, "paper not found")  # 404, not 403: do not reveal other users' papers exist
        return p  # type: ignore[return-value]

    def run_for(user: User, run_id: str) -> RunState:
        s = c.runs.get(run_id)
        if s is None or not visible(user, c.papers.get(s.paper_id)):
            raise HTTPException(404, "run not found")
        return s

    def orch():  # type: ignore[no-untyped-def]
        if c.orchestrator is None:
            raise HTTPException(503, "no LLM is configured (set CG_LLM_CHAIN); reading data still works")
        return c.orchestrator

    def latest_run(user: User, paper_id: str) -> RunState:
        paper_for(user, paper_id)
        runs = c.runs.list(paper_id)
        if not runs:
            raise HTTPException(404, "no run for this paper yet")
        return runs[-1]  # type: ignore[no-any-return]

    # ---- papers --------------------------------------------------------------------------------------------
    @r.post("/papers", status_code=201)
    async def upload_paper(request: Request, user: User = Depends(writer)) -> dict[str, Any]:
        """Body: the raw PDF bytes (Content-Type: application/pdf). Optional header X-Filename."""
        data = await request.body()
        if not data or len(data) > MAX_PDF_BYTES:
            raise HTTPException(413 if data else 422, "PDF is empty" if not data else "PDF too large")
        try:
            ingested = ingest_pdf(data)
        except PdfError as e:
            raise HTTPException(422, str(e)) from e
        rec = PaperRecord(paper_hash=ingested.paper_hash, filename=request.headers.get("x-filename", ""), size=len(data),
                          owner_id=None if user.id == "local" else user.id, pages=ingested.n_pages)
        c.papers.add(rec, data)
        return {"paper_id": rec.id, "paper_hash": rec.paper_hash, "pages": ingested.n_pages, "needs_ocr": ingested.needs_ocr}

    @r.get("/papers")
    def list_papers(user: User = Depends(cur)) -> list[dict[str, Any]]:
        out = []
        for p in c.papers.list():
            if not visible(user, p):
                continue
            runs = c.runs.list(p.id)
            out.append({**p.model_dump(mode="json"), "title": _paper_title(p, runs), "latest_run": _run_brief(runs[-1]) if runs else None})
        return out

    @r.get("/papers/{paper_id}")
    def get_paper(paper_id: str, user: User = Depends(cur)) -> dict[str, Any]:
        p = paper_for(user, paper_id)
        runs = c.runs.list(paper_id)
        return {**p.model_dump(mode="json"), "title": _paper_title(p, runs), "runs": [_summary(s) for s in runs]}

    @r.delete("/papers/{paper_id}", status_code=204)
    def delete_paper(paper_id: str, user: User = Depends(writer)) -> Response:
        """Erase a paper and everything stored for it: runs, gates, jobs, events (worker output) and the model-call log (prompts
        contain the paper text). A run that is executing right now blocks the delete (409): it would write its state back."""
        paper_for(user, paper_id)
        runs = c.runs.list(paper_id)
        if any(s.status == "running" for s in runs):
            raise HTTPException(409, "an analysis is running for this paper right now; try again in a moment")
        ids = [s.run_id for s in runs]
        c.gate_repo.delete_for_runs(ids)
        c.jobs.repo.delete_for_runs(ids)
        c.events.delete_runs(ids)
        if c.audit_log is not None:
            c.audit_log.delete_runs(ids)
        c.runs.delete_for_paper(paper_id)
        SqlGraphRepo(c.session_factory).delete_for_paper(paper_id)
        c.papers.delete(paper_id)
        return Response(status_code=204)

    def stored_text(paper_id: str) -> Any:
        pdf = c.papers.get_pdf(paper_id)
        if pdf is None:
            raise HTTPException(404, "paper not found")
        try:
            return ingest_pdf(pdf)
        except PdfError as e:
            raise HTTPException(422, str(e)) from e

    @r.get("/papers/{paper_id}/text")
    def paper_text(paper_id: str, user: User = Depends(cur)) -> dict[str, Any]:
        paper_for(user, paper_id)
        ing = stored_text(paper_id)
        return {"pages": ing.pages, "chars": len(ing.text), "needs_ocr": ing.needs_ocr}

    @r.get("/papers/{paper_id}/understanding")
    def understanding(paper_id: str, user: User = Depends(cur)) -> dict[str, Any]:
        """What the analysis extracted from the paper (claims, setup, datasets, metrics with their quotes), or null before it has run."""
        p = paper_for(user, paper_id)
        runs = c.runs.list(paper_id)
        s = runs[-1] if runs else None
        ing = stored_text(paper_id)
        return {"paper": {**p.model_dump(mode="json"), "pages": ing.n_pages, "chars": len(ing.text), "needs_ocr": ing.needs_ocr},
                "run": _summary(s) if s else None, "parsed": s.parsed.model_dump(mode="json") if s and s.parsed else None,
                "llm_configured": c.orchestrator is not None}

    @r.get("/papers/{paper_id}/artifacts")
    def artifacts(paper_id: str, user: User = Depends(cur)) -> dict[str, Any]:
        paper_for(user, paper_id)
        runs = c.runs.list(paper_id)
        s = runs[-1] if runs else None
        return {"run": _summary(s) if s else None, "inventory": s.inventory.model_dump(mode="json") if s and s.inventory else None}

    @r.get("/history")
    def history(paper_id: str | None = None, limit: int = 500, user: User = Depends(cur)) -> dict[str, Any]:
        """Everything the backend did for a paper (or for all your papers), oldest first: steps, gates and your answers, jobs, verdicts."""
        papers = [paper_for(user, paper_id)] if paper_id else [p for p in c.papers.list() if visible(user, p)]
        limit = max(1, min(limit, 2000))
        events: list[dict[str, Any]] = []
        for p in papers:
            title = _paper_title(p, c.runs.list(p.id))
            for s in c.runs.list(p.id):
                for e in c.events.list(s.run_id, limit=5000):
                    events.append({"id": e.id, "ts": e.ts.isoformat(), "run_id": e.run_id, "paper_id": p.id, "paper": title,
                                   "module": e.module, "kind": e.kind, "payload": e.payload})
        events.sort(key=lambda e: (e["ts"], e["id"]))
        return {"events": events[-limit:], "truncated": len(events) > limit}

    @r.get("/overview")
    def overview(user: User = Depends(cur)) -> dict[str, Any]:
        """Mission Control: counts and the things waiting for you, over the papers you can see."""
        papers = [p for p in c.papers.list() if visible(user, p)]
        runs_by_paper = {p.id: c.runs.list(p.id) for p in papers}
        all_runs = [s for rs in runs_by_paper.values() for s in rs]
        latest = {pid: rs[-1] for pid, rs in runs_by_paper.items() if rs}
        claims = dict.fromkeys(VERDICTS, 0)
        gaps = {"blocking": 0, "major": 0, "minor": 0}
        recent: list[tuple[Any, dict[str, Any]]] = []
        for p in papers:
            s = latest.get(p.id)
            if s is None:
                continue
            for g in s.gaps:
                gaps[g.severity] += 1
            by_claim = {v.claim_id: v for v in s.verdicts}
            for cl in (s.parsed.claims if s.parsed else []):
                v = by_claim.get(cl.id)
                claims[v.status if v else "not_executed"] += 1
                recent.append((s.updated_at, {"paper_id": p.id, "paper": _paper_title(p, runs_by_paper[p.id]), "text": cl.text,
                                              "reported": cl.reported_value, "measured_mean": v.measured_mean if v else None,
                                              "status": v.status if v else "not_executed"}))
        recent.sort(key=lambda t: t[0], reverse=True)
        titles = {p.id: _paper_title(p, runs_by_paper[p.id]) for p in papers}
        mine = {s.run_id for s in all_runs}
        gates = [{"id": g.id, "run_id": g.run_id, "kind": g.kind, "paper_id": c.runs.get(g.run_id).paper_id,  # type: ignore[union-attr]
                  "paper": titles.get(c.runs.get(g.run_id).paper_id, "paper")}  # type: ignore[union-attr]
                 for g in c.gate_repo.list(status="open") if g.run_id in mine]
        workers = c.jobs.repo.list_workers()
        now = utcnow()
        return {
            "papers": len(papers),
            "runs": {"total": len(all_runs), **{k: sum(s.status == k for s in all_runs) for k in
                                                ("running", "waiting_gate", "waiting_jobs", "completed", "failed")}},
            "claims": {"total": sum(claims.values()), **claims},
            "gaps": gaps,
            "stages": {"parsed": sum(s.parsed is not None for s in latest.values()),
                       "audited": sum(s.inventory is not None for s in latest.values()),
                       "executed": sum(s.execution == "clean_runs" for s in latest.values())},
            "open_gates": gates,
            "workers": {"total": len(workers), "online": sum((now - w.last_seen) < timedelta(seconds=90) for w in workers)},
            "llm_configured": c.orchestrator is not None,
            "recent_claims": [x[1] for x in recent[:8]],
        }

    @r.post("/papers/{paper_id}/runs", status_code=202)
    def start_run(paper_id: str, bg: BackgroundTasks, body: StartRun | None = None, user: User = Depends(writer)) -> dict[str, str]:
        paper_for(user, paper_id)
        o = orch()
        try:
            cells = parse_notebook(body.notebook) if body and body.notebook else None
            state = o.start(paper_id, notebook=cells, seeds=body.seeds if body else None)
        except NotebookError as e:
            raise HTTPException(422, f"invalid notebook: {e}") from e
        except KeyError as e:
            raise HTTPException(404, "paper not found") from e
        bg.add_task(o.advance, state.run_id)
        return {"run_id": state.run_id}

    # ---- runs / events -------------------------------------------------------------------------------------
    @r.get("/runs/{run_id}")
    def get_run(run_id: str, user: User = Depends(cur)) -> dict[str, Any]:
        return _summary(run_for(user, run_id))

    @r.get("/runs/{run_id}/events")
    async def run_events(run_id: str, follow: bool = True, after: str | None = None, user: User = Depends(cur)) -> StreamingResponse:
        """Server-sent events. With follow=true (default) the stream stays open until the run completes or fails; a
        run paused at a gate keeps streaming so the UI sees `gate_opened` and what happens after it is answered."""
        run_for(user, run_id)

        async def gen() -> AsyncIterator[str]:
            last = after
            while True:
                batch = c.events.list(run_id, after_id=last)
                for e in batch:
                    last = e.id
                    yield _sse(e.kind, e.id, {"ts": e.ts.isoformat(), "module": e.module, **e.payload})
                state = c.runs.get(run_id)
                if not follow or (not batch and state is not None and state.status in ("completed", "failed")):
                    return
                if not batch:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # ---- gates ---------------------------------------------------------------------------------------------
    @r.get("/gates")
    def list_gates(run_id: str | None = None, status: str | None = "open", user: User = Depends(cur)) -> list[dict[str, Any]]:
        def ok(g: Any) -> bool:
            s = c.runs.get(g.run_id)
            return s is not None and visible(user, c.papers.get(s.paper_id))
        return [g.model_dump(mode="json") for g in c.gate_repo.list(run_id=run_id, status=status) if ok(g)]

    @r.post("/gates/{gate_id}/respond")
    def respond(gate_id: str, body: GateResponse, bg: BackgroundTasks, user: User = Depends(writer)) -> dict[str, Any]:
        g = c.gate_repo.get(gate_id)
        if g is None:
            raise HTTPException(404, "gate not found")
        run_for(user, g.run_id)
        o = orch()
        try:
            state = o.respond(gate_id, body.action, body.payload)
        except KeyError as e:
            raise HTTPException(404, "gate not found") from e
        except GateAlreadyResolved as e:
            raise HTTPException(409, str(e)) from e
        except GateError as e:
            raise HTTPException(422, str(e)) from e
        bg.add_task(o.advance, state.run_id)
        return {"run_id": state.run_id, "resumed": True}

    r.include_router(build_graph_router(c, SqlGraphRepo(c.session_factory), paper_for, orch))

    # ---- results -------------------------------------------------------------------------------------------
    @r.get("/papers/{paper_id}/claims")
    def claims(paper_id: str, user: User = Depends(cur)) -> list[dict[str, Any]]:
        s = latest_run(user, paper_id)
        # a verdict exists only if clean runs were executed; otherwise every claim honestly reports `not_executed`
        by_claim = {v.claim_id: v for v in s.verdicts}
        return [{"claim": cl.model_dump(mode="json"),
                 "verdict": by_claim[cl.id].model_dump(mode="json") if cl.id in by_claim
                 else {"status": "not_executed", "caveats": ["no experiment was run"]}}
                for cl in (s.parsed.claims if s.parsed else [])]

    @r.get("/papers/{paper_id}/audit")
    def audit(paper_id: str, user: User = Depends(cur)) -> list[dict[str, Any]]:
        return [g.model_dump(mode="json") for g in latest_run(user, paper_id).gaps]

    @r.get("/reports/{paper_id}", response_model=None)
    def report(paper_id: str, format: str = "md", user: User = Depends(cur)) -> Any:  # noqa: A002
        s = latest_run(user, paper_id)
        if s.report is None:
            raise HTTPException(404, "no report yet")
        if format == "md":
            return PlainTextResponse(s.report, media_type="text/markdown")
        if format == "json":
            return {"markdown": s.report, "caveats": s.caveats, "run": _summary(s)}
        raise HTTPException(422, "format must be md or json (PDF export is not built yet)")

    return r
