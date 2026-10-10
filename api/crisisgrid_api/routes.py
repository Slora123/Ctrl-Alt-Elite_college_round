"""REST + SSE endpoints (system_design 2). Thin: everything real happens in the orchestrator."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .container import AppContainer
from .ingest.pdf import PdfError, ingest_pdf
from .orchestration.gates import GateAlreadyResolved, GateError
from .orchestration.models import PaperRecord, RunState

MAX_PDF_BYTES = 30 * 1024 * 1024


class GateResponse(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


def _summary(s: RunState) -> dict[str, Any]:
    return {"run_id": s.run_id, "paper_id": s.paper_id, "status": s.status, "node": s.node, "waiting_gate_id": s.waiting_gate_id,
            "error": s.error, "route": s.route.model_dump(mode="json") if s.route else None, "needs_ocr": s.needs_ocr,
            "caveats": s.caveats}


def _sse(kind: str, event_id: str, data: dict[str, Any]) -> str:
    return f"id: {event_id}\nevent: {kind}\ndata: {json.dumps(data, default=str)}\n\n"


def build_router(c: AppContainer) -> APIRouter:
    r = APIRouter()

    def orch():  # type: ignore[no-untyped-def]
        if c.orchestrator is None:
            raise HTTPException(503, "no LLM is configured (set CG_LLM_CHAIN); reading data still works")
        return c.orchestrator

    def latest_run(paper_id: str) -> RunState:
        if c.papers.get(paper_id) is None:
            raise HTTPException(404, "paper not found")
        runs = c.runs.list(paper_id)
        if not runs:
            raise HTTPException(404, "no run for this paper yet")
        return runs[-1]  # type: ignore[no-any-return]

    # ---- papers --------------------------------------------------------------------------------------------
    @r.post("/papers", status_code=201)
    async def upload_paper(request: Request) -> dict[str, Any]:
        """Body: the raw PDF bytes (Content-Type: application/pdf). Optional header X-Filename."""
        data = await request.body()
        if not data or len(data) > MAX_PDF_BYTES:
            raise HTTPException(413 if data else 422, "PDF is empty" if not data else "PDF too large")
        try:
            ingested = ingest_pdf(data)
        except PdfError as e:
            raise HTTPException(422, str(e)) from e
        rec = PaperRecord(paper_hash=ingested.paper_hash, filename=request.headers.get("x-filename", ""), size=len(data))
        c.papers.add(rec, data)
        return {"paper_id": rec.id, "paper_hash": rec.paper_hash, "pages": ingested.n_pages, "needs_ocr": ingested.needs_ocr}

    @r.get("/papers")
    def list_papers() -> list[dict[str, Any]]:
        return [p.model_dump(mode="json") for p in c.papers.list()]

    @r.get("/papers/{paper_id}")
    def get_paper(paper_id: str) -> dict[str, Any]:
        p = c.papers.get(paper_id)
        if p is None:
            raise HTTPException(404, "paper not found")
        return {**p.model_dump(mode="json"), "runs": [_summary(s) for s in c.runs.list(paper_id)]}

    @r.post("/papers/{paper_id}/runs", status_code=202)
    def start_run(paper_id: str, bg: BackgroundTasks) -> dict[str, str]:
        o = orch()
        try:
            state = o.start(paper_id)
        except KeyError as e:
            raise HTTPException(404, "paper not found") from e
        bg.add_task(o.advance, state.run_id)
        return {"run_id": state.run_id}

    # ---- runs / events -------------------------------------------------------------------------------------
    @r.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        s = c.runs.get(run_id)
        if s is None:
            raise HTTPException(404, "run not found")
        return _summary(s)

    @r.get("/runs/{run_id}/events")
    async def run_events(run_id: str, follow: bool = True, after: str | None = None) -> StreamingResponse:
        """Server-sent events. With follow=true (default) the stream stays open until the run completes or fails; a
        run paused at a gate keeps streaming so the UI sees `gate_opened` and what happens after it is answered."""
        if c.runs.get(run_id) is None:
            raise HTTPException(404, "run not found")

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
    def list_gates(run_id: str | None = None, status: str | None = "open") -> list[dict[str, Any]]:
        return [g.model_dump(mode="json") for g in c.gate_repo.list(run_id=run_id, status=status)]

    @r.post("/gates/{gate_id}/respond")
    def respond(gate_id: str, body: GateResponse, bg: BackgroundTasks) -> dict[str, Any]:
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

    # ---- results -------------------------------------------------------------------------------------------
    @r.get("/papers/{paper_id}/claims")
    def claims(paper_id: str) -> list[dict[str, Any]]:
        s = latest_run(paper_id)
        # verdicts need clean runs (P6+): until then every claim honestly reports `not_executed`
        return [{"claim": cl.model_dump(mode="json"), "verdict": {"status": "not_executed", "caveats": ["no experiment was run"]}}
                for cl in (s.parsed.claims if s.parsed else [])]

    @r.get("/papers/{paper_id}/audit")
    def audit(paper_id: str) -> list[dict[str, Any]]:
        return [g.model_dump(mode="json") for g in latest_run(paper_id).gaps]

    @r.get("/reports/{paper_id}", response_model=None)
    def report(paper_id: str, format: str = "md") -> Any:  # noqa: A002
        s = latest_run(paper_id)
        if s.report is None:
            raise HTTPException(404, "no report yet")
        if format == "md":
            return PlainTextResponse(s.report, media_type="text/markdown")
        if format == "json":
            return {"markdown": s.report, "caveats": s.caveats, "run": _summary(s)}
        raise HTTPException(422, "format must be md or json (PDF export is not built yet)")

    return r
