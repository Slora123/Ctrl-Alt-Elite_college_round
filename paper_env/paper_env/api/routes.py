"""Thin HTTP handlers. All logic lives in services; handlers translate and delegate."""
from __future__ import annotations

import asyncio
import queue
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ..container import App
from ..domain import (
    Author, DatasetManifest, DecisionKind, EnvSpec, EventType, RunMode, new_id,
)
from ..events import json_schemas
from ..hooks import Decision
from . import schemas as S
from .deps import get_app, jsonable

router = APIRouter()
A = Depends(get_app)


@router.get("/health")
def health(app: App = A):
    return {"status": "ok", "backend": app.backend.capabilities()}


# ------------------------------------------------------------------ paper envs
@router.post("/paper-envs", status_code=201)
def create_paper_env(body: S.CreatePaperEnv, app: App = A):
    spec = EnvSpec(python_version=body.python_version, requirements=body.requirements, pins=body.pins,
                   system_deps=body.system_deps, seed=body.seed)
    env = app.paper_envs.create(body.alias, body.paper_hash, spec, body.dataset_ids)
    result = jsonable(app.envs.create_env(env.id)) if body.build_env else None
    return {"paper_env": jsonable(app.paper_envs.get(env.id)), "env_build": result}


@router.get("/paper-envs")
def list_paper_envs(app: App = A):
    return jsonable(app.paper_envs.list())


@router.get("/paper-envs/{paper_env_id}")
def get_paper_env(paper_env_id: str, app: App = A):
    return {"paper_env": jsonable(app.paper_envs.get(paper_env_id)), "env_spec": jsonable(app.paper_envs.get_spec(paper_env_id)),
            "active_run_id": app.runs.active_run_id(paper_env_id)}


@router.delete("/paper-envs/{paper_env_id}", status_code=204)
def delete_paper_env(paper_env_id: str, app: App = A):
    if app.runs.active_run_id(paper_env_id):
        raise HTTPException(409, "a run is active; interrupt it first")
    app.runner.shutdown(paper_env_id)
    app.paper_envs.delete(paper_env_id)


@router.put("/paper-envs/{paper_env_id}/datasets")
def set_datasets(paper_env_id: str, body: S.SetDatasets, app: App = A):
    return jsonable(app.paper_envs.set_datasets(paper_env_id, body.dataset_ids))


# ------------------------------------------------------------------ env operations (never via cells)
@router.post("/paper-envs/{paper_env_id}/env/create")
def env_create(paper_env_id: str, app: App = A):
    return jsonable(app.envs.create_env(paper_env_id))


@router.post("/paper-envs/{paper_env_id}/env/install")
def env_install(paper_env_id: str, body: S.InstallReq, app: App = A):
    return jsonable(app.envs.install_package(paper_env_id, body.name, body.version))


@router.post("/paper-envs/{paper_env_id}/env/pin")
def env_pin(paper_env_id: str, body: S.PinReq, app: App = A):
    return jsonable(app.envs.pin_version(paper_env_id, body.name, body.version))


@router.post("/paper-envs/{paper_env_id}/env/rebuild")
def env_rebuild(paper_env_id: str, app: App = A):
    return jsonable(app.envs.rebuild_env(paper_env_id))


@router.get("/paper-envs/{paper_env_id}/env/lockfile")
def env_lockfile(paper_env_id: str, app: App = A):
    return jsonable(app.envs.get_lockfile(paper_env_id))


@router.get("/paper-envs/{paper_env_id}/env/hardware")
def env_hardware(paper_env_id: str, app: App = A):
    return app.envs.hardware_info(paper_env_id)


# ------------------------------------------------------------------ notebook
@router.get("/paper-envs/{paper_env_id}/cells")
def list_cells(paper_env_id: str, app: App = A):
    return jsonable(app.notebook.list_cells(paper_env_id))


@router.post("/paper-envs/{paper_env_id}/cells", status_code=201)
def add_cell(paper_env_id: str, body: S.AddCell, app: App = A):
    return jsonable(app.notebook.add_cell(paper_env_id, body.source, body.author, position=body.position,
                                          declared_inputs=body.declared_inputs, declared_outputs=body.declared_outputs,
                                          node_id=body.node_id, source_span=body.source_span))


@router.get("/cells/{cell_id}")
def get_cell(cell_id: str, app: App = A):
    return {"cell": jsonable(app.notebook.get_cell(cell_id)), "source": app.notebook.source_of(cell_id)}


@router.put("/cells/{cell_id}/source")
def edit_cell(cell_id: str, body: S.EditCell, app: App = A):
    """The single edit door: creates a new CellVersion; cell and downstream cells become stale."""
    return jsonable(app.notebook.apply_edit(cell_id, body.source, body.author))


@router.patch("/cells/{cell_id}/declarations")
def cell_declarations(cell_id: str, body: S.Declarations, app: App = A):
    return jsonable(app.notebook.update_declarations(cell_id, declared_inputs=body.declared_inputs,
                                                     declared_outputs=body.declared_outputs))


@router.delete("/cells/{cell_id}", status_code=204)
def delete_cell(cell_id: str, app: App = A):
    app.notebook.delete_cell(cell_id)


@router.get("/cells/{cell_id}/versions")
def cell_versions(cell_id: str, app: App = A):
    return jsonable(app.notebook.versions(cell_id))


@router.post("/cell-versions/{version_id}/revert")
def revert(version_id: str, body: S.RevertReq, app: App = A):
    return jsonable(app.notebook.revert_to(version_id, body.author))


# ------------------------------------------------------------------ runs
@router.post("/runs", status_code=202)
def start_run(body: S.StartRun, app: App = A):
    mode = RunMode(body.mode)
    if mode in (RunMode.CELL, RunMode.UNTIL) and not body.cell_id:
        raise HTTPException(422, f"mode '{body.mode}' requires cell_id")
    rid = new_id()
    common: dict[str, Any] = {"seed": body.seed, "run_id": rid}
    if mode == RunMode.CELL:
        fn = lambda: app.runs.run_cell(body.cell_id, timeout=body.cell_timeout, **common)  # noqa: E731
    else:
        common.update(cell_timeout=body.cell_timeout, run_timeout=body.run_timeout, checkpoint=body.checkpoint,
                      stop_on_fail=body.stop_on_fail)
        if mode == RunMode.UNTIL:
            fn = lambda: app.runs.run_until(body.cell_id, **common)  # noqa: E731
        elif mode == RunMode.ALL:
            fn = lambda: app.runs.run_all(body.paper_env_id, **common)  # noqa: E731
        else:
            fn = lambda: app.runs.run_clean(body.paper_env_id, **common)  # noqa: E731
    if app.runs.active_run_id(body.paper_env_id):
        raise HTTPException(409, f"run {app.runs.active_run_id(body.paper_env_id)} is still active")
    if body.wait:
        return jsonable(fn())
    app.run_in_background(fn)
    return {"run_id": rid, "status": "accepted"}


@router.get("/runs/{run_id}")
def get_run(run_id: str, app: App = A):
    return jsonable(app.runs.get_run(run_id))


@router.get("/runs/{run_id}/cells")
def run_cells(run_id: str, app: App = A):
    return jsonable(app.runs.cell_runs(run_id))


@router.get("/paper-envs/{paper_env_id}/runs")
def list_runs(paper_env_id: str, app: App = A):
    return jsonable(app.runs.list_runs(paper_env_id))


@router.post("/runs/{run_id}/interrupt")
def interrupt_run(run_id: str, app: App = A):
    return {"interrupted": app.runs.interrupt(run_id)}


@router.post("/paper-envs/{paper_env_id}/kernel/restart")
def restart_kernel(paper_env_id: str, app: App = A):
    if app.runs.active_run_id(paper_env_id):
        raise HTTPException(409, "a run is active; interrupt it first")
    s = app.runner.restart_kernel(paper_env_id)
    return {"session_id": s.id, "note": "all executed cells are now stale"}


# ------------------------------------------------------------------ editor / interactivity
@router.post("/paper-envs/{paper_env_id}/kernel/complete")
def kernel_complete(paper_env_id: str, body: S.CodePos, app: App = A):
    return app.runner.complete(paper_env_id, body.code, body.cursor_pos)


@router.post("/paper-envs/{paper_env_id}/kernel/inspect")
def kernel_inspect(paper_env_id: str, body: S.CodePos, app: App = A):
    return app.runner.inspect(paper_env_id, body.code, body.cursor_pos)


@router.post("/paper-envs/{paper_env_id}/kernel/input", status_code=204)
def kernel_input(paper_env_id: str, body: S.InputReq, app: App = A):
    """Answer an ``input_requested`` event."""
    app.runner.provide_input(paper_env_id, body.text)


@router.post("/paper-envs/{paper_env_id}/kernel/comm", status_code=204)
def kernel_comm(paper_env_id: str, body: S.CommReq, app: App = A):
    """Frontend -> kernel widget traffic (kernel -> frontend arrives as ephemeral ``comm_message`` events)."""
    app.runner.comm_send(paper_env_id, body.msg_type, body.content, body.buffers)


# ------------------------------------------------------------------ datasets
@router.post("/datasets", status_code=201)
def register_dataset(body: S.RegisterDataset, app: App = A):
    return jsonable(app.datasets.register(DatasetManifest(**body.model_dump())))


@router.post("/datasets/upload", status_code=201)
def upload_dataset(body: S.UploadDataset, app: App = A):
    from pathlib import Path
    return jsonable(app.datasets.register_upload(body.name, body.version, Path(body.path), author=body.author,
                                                 license=body.license, splits=body.splits))


@router.get("/datasets")
def list_datasets(app: App = A):
    return jsonable(app.datasets.list())


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, app: App = A):
    return jsonable(app.datasets.get(dataset_id))


@router.get("/datasets/{dataset_id}/status")
def dataset_status(dataset_id: str, app: App = A):
    return {"dataset_id": dataset_id, "status": app.datasets.status(dataset_id).value}


@router.post("/datasets/{dataset_id}/fetch")
def fetch_dataset(dataset_id: str, app: App = A):
    return jsonable(app.datasets.fetch(dataset_id))


@router.post("/datasets/{dataset_id}/verify")
def verify_dataset(dataset_id: str, app: App = A):
    return jsonable(app.datasets.verify(dataset_id))


@router.post("/datasets/{dataset_id}/check-upstream")
def check_upstream(dataset_id: str, app: App = A):
    return jsonable(app.datasets.check_upstream(dataset_id))


@router.post("/datasets/{dataset_id}/reset")
def reset_dataset(dataset_id: str, body: S.ResetDataset, app: App = A):
    """Explicit operator action; nothing refreshes a corrupted / changed dataset automatically."""
    return jsonable(app.datasets.reset(dataset_id, body.accept_upstream))


# ------------------------------------------------------------------ checkpoints
@router.get("/paper-envs/{paper_env_id}/checkpoints")
def list_checkpoints(paper_env_id: str, run_id: str | None = None, app: App = A):
    return jsonable(app.checkpoints.list(paper_env_id, run_id))


@router.get("/checkpoints/{checkpoint_id}")
def get_checkpoint(checkpoint_id: str, app: App = A):
    return jsonable(app.checkpoints.get(checkpoint_id))


@router.post("/checkpoints/{checkpoint_id}/resume")
def resume_checkpoint(checkpoint_id: str, body: S.ResumeReq, app: App = A):
    cp = app.checkpoints.get(checkpoint_id)
    if app.runs.active_run_id(cp.paper_env_id):
        raise HTTPException(409, "a run is active; interrupt it first")
    if body.continue_run:
        res, run = app.runs.continue_from(checkpoint_id, stop_on_fail=body.stop_on_fail)
        return {"resume": jsonable(res), "run": jsonable(run)}
    return {"resume": jsonable(app.checkpoints.resume_from(checkpoint_id)), "run": None}


# ------------------------------------------------------------------ harness seam: blocked actions
@router.get("/blocked")
def list_blocked(paper_env_id: str | None = None, app: App = A):
    return jsonable(app.gate.list_pending(paper_env_id))


@router.post("/blocked/{action_id}/resolve")
def resolve_blocked(action_id: str, body: S.ResolveBlocked, app: App = A):
    kind = DecisionKind.ALLOW if body.decision == "allow" else DecisionKind.DENY
    return {"result": jsonable(app.gate.resume_blocked(action_id, Decision(kind, body.reason)))}


# ------------------------------------------------------------------ files workspace
@router.get("/paper-envs/{paper_env_id}/files")
def list_files(paper_env_id: str, app: App = A):
    app.paper_envs.get(paper_env_id)
    return jsonable(app.files.list(paper_env_id))


@router.put("/paper-envs/{paper_env_id}/files/{path:path}", status_code=201)
async def upload_file(paper_env_id: str, path: str, request: Request, app: App = A):
    """Raw request body is the file content (stream-written; size-capped; path confined to the workspace)."""
    app.paper_envs.get(paper_env_id)
    chunks = [c async for c in request.stream()]
    return jsonable(app.files.save(paper_env_id, path, chunks))


@router.get("/paper-envs/{paper_env_id}/files/{path:path}")
def download_file(paper_env_id: str, path: str, app: App = A):
    p = app.files.open_for_download(paper_env_id, path)
    return FileResponse(p, filename=p.name)


@router.delete("/paper-envs/{paper_env_id}/files/{path:path}", status_code=204)
def delete_file(paper_env_id: str, path: str, app: App = A):
    app.files.delete(paper_env_id, path)


# ------------------------------------------------------------------ events
@router.get("/events/history")
def event_history(after_seq: int = 0, limit: int = Query(1000, le=10000), run_id: str | None = None,
                  paper_env_id: str | None = None, types: list[EventType] | None = Query(None), app: App = A):
    return jsonable(app.bus.history(after_seq=after_seq, limit=limit, run_id=run_id, paper_env_id=paper_env_id,
                                    types=types))


@router.get("/events/schema")
def event_schema():
    return json_schemas()


@router.websocket("/events")
async def events_ws(ws: WebSocket, after_seq: int = 0, run_id: str | None = None, paper_env_id: str | None = None,
                    types: list[EventType] | None = Query(None)):
    """Replay persisted events after ``after_seq`` then stream live ones. Each message is one event JSON with
    its ``seq``; clients reconnect with the last seq they saw (at-least-once, ordered per connection)."""
    app: App = ws.app.state.core
    await ws.accept()
    q, sub = app.bus.subscribe_queue(types=types, run_id=run_id, paper_env_id=paper_env_id)  # subscribe first: no gap
    last = after_seq
    try:
        while True:  # replay in pages
            page = await asyncio.to_thread(app.bus.history, last, 500, run_id=run_id, paper_env_id=paper_env_id, types=types)
            for e in page:
                await ws.send_json(jsonable(e))
                last = max(last, e.seq)
            if len(page) < 500:
                break
        while True:
            try:
                e = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                continue
            if e.seq is not None and e.seq <= last:
                continue  # already delivered during replay
            await ws.send_json(jsonable(e))
            last = max(last, e.seq or last)
    except WebSocketDisconnect:
        pass
    finally:
        sub.close()
