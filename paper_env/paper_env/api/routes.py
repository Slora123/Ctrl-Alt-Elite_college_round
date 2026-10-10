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
from ..auth import Principal
from ..backends.registry import RuntimeUnavailable
from ..domain import ActionKind, SessionLimits
from ..hooks import Action, ActionBlocked
from ..security.store import SecretError
from ..services.terminal import TerminalError
from .deps import authorize, can_see, check_env_access, get_app, jsonable, visible_env_ids

router = APIRouter(dependencies=[Depends(authorize)])
public = APIRouter()  # no authentication: liveness only
A = Depends(get_app)
P = Depends(authorize)


@public.get("/health")
def health(app: App = A):
    return {"status": "ok", "backend": app.backend.capabilities()}


# ------------------------------------------------------------------ paper envs
@router.post("/paper-envs", status_code=201)
def create_paper_env(body: S.CreatePaperEnv, app: App = A, who: Principal = P):
    spec = EnvSpec(python_version=body.python_version, requirements=body.requirements, pins=body.pins,
                   system_deps=body.system_deps, seed=body.seed)
    env = app.paper_envs.create(body.alias, body.paper_hash, spec, body.dataset_ids,
                                owner_id=None if who.id == "local" else who.id)
    result = jsonable(app.envs.create_env(env.id)) if body.build_env else None
    return {"paper_env": jsonable(app.paper_envs.get(env.id)), "env_build": result}


@router.get("/me")
def me(who: Principal = P):
    return {"id": who.id, "name": who.name, "role": who.role}


@router.get("/paper-envs")
def list_paper_envs(app: App = A, who: Principal = P):
    return jsonable([e for e in app.paper_envs.list() if can_see(who, e.owner_id)])


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
                                          node_id=body.node_id, source_span=body.source_span, cell_type=body.cell_type))


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
def start_run(body: S.StartRun, app: App = A, who: Principal = P):
    check_env_access(app, who, body.paper_env_id)  # ids in the body are invisible to the router-level check
    if body.cell_id:
        cell_env = app.notebook.get_cell(body.cell_id).paper_env_id
        check_env_access(app, who, cell_env)
        if cell_env != body.paper_env_id:
            raise HTTPException(422, "cell_id does not belong to paper_env_id")
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
def list_blocked(paper_env_id: str | None = None, app: App = A, who: Principal = P):
    visible = visible_env_ids(app, who)
    return jsonable([b for b in app.gate.list_pending(paper_env_id)
                     if visible is None or b.paper_env_id is None or b.paper_env_id in visible])


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
                  paper_env_id: str | None = None, types: list[EventType] | None = Query(None), app: App = A,
                  who: Principal = P):
    visible = visible_env_ids(app, who)
    evs = app.bus.history(after_seq=after_seq, limit=limit, run_id=run_id, paper_env_id=paper_env_id, types=types)
    return jsonable([e for e in evs if visible is None or e.paper_env_id is None or e.paper_env_id in visible])


@router.get("/events/schema")
def event_schema():
    return json_schemas()


@router.websocket("/events")
async def events_ws(ws: WebSocket, after_seq: int = 0, run_id: str | None = None, paper_env_id: str | None = None,
                    types: list[EventType] | None = Query(None), who: Principal = P):
    """Replay persisted events after ``after_seq`` then stream live ones. Each message is one event JSON with
    its ``seq``; clients reconnect with the last seq they saw (at-least-once, ordered per connection)."""
    app: App = ws.app.state.core
    await ws.accept()
    q, sub = app.bus.subscribe_queue(types=types, run_id=run_id, paper_env_id=paper_env_id)  # subscribe first: no gap
    visible = visible_env_ids(app, who)
    ok = lambda e: visible is None or e.paper_env_id is None or e.paper_env_id in visible  # noqa: E731
    last = after_seq
    try:
        while True:  # replay in pages
            page = await asyncio.to_thread(app.bus.history, last, 500, run_id=run_id, paper_env_id=paper_env_id, types=types)
            for e in page:
                if ok(e):
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
            if not ok(e):
                continue
            await ws.send_json(jsonable(e))
            last = max(last, e.seq or last)
    except WebSocketDisconnect:
        pass
    finally:
        sub.close()


# ------------------------------------------------------------------ runtime profiles, limits, session
@router.get("/runtimes")
def list_runtimes(app: App = A):
    return [p.as_dict() for p in app.registry.profiles()]


@router.put("/paper-envs/{paper_env_id}/runtime")
def set_runtime(paper_env_id: str, body: S.SetRuntime, app: App = A):
    """Change the runtime type (CPU / GPU / TPU / AWS ...). Unavailable profiles are refused with the reason. The
    running kernel (if any) is ended: its in-memory state belongs to the old runtime, so cells go stale."""
    app.registry.backend_for(body.runtime)  # raises RuntimeUnavailable with the reason
    if app.runs.active_run_id(paper_env_id):
        raise HTTPException(409, "a run is active; interrupt it first")
    env = app.paper_envs.set_runtime(paper_env_id, body.runtime)
    app.runner.expire(paper_env_id, "runtime_changed", f"runtime changed to {body.runtime}")
    return jsonable(env)


@router.get("/paper-envs/{paper_env_id}/limits")
def get_limits(paper_env_id: str, app: App = A):
    return jsonable(app.paper_envs.get(paper_env_id).limits)


@router.put("/paper-envs/{paper_env_id}/limits")
def set_limits(paper_env_id: str, body: S.Limits, app: App = A):
    return jsonable(app.paper_envs.set_limits(paper_env_id, SessionLimits(**body.model_dump())).limits)


@router.get("/paper-envs/{paper_env_id}/session")
def session_info(paper_env_id: str, app: App = A):
    env = app.paper_envs.get(paper_env_id)
    return {"runtime": env.runtime, "limits": jsonable(env.limits), "session": app.runner.session_info(paper_env_id),
            "active_run_id": app.runs.active_run_id(paper_env_id)}


# ------------------------------------------------------------------ secrets (names only; values never leave the server)
@router.get("/secrets")
def list_secrets(app: App = A, who: Principal = P):
    return app.secrets_store.list(who.id)


@router.put("/secrets/{name}")
def put_secret(name: str, body: S.PutSecret, app: App = A, who: Principal = P):
    meta = app.secrets_store.put(who.id, name, body.value, allow_in_cells=body.allow_in_cells)
    app.scrubber.add_secrets([body.value])  # from now on it is redacted everywhere
    return meta


@router.patch("/secrets/{name}")
def grant_secret(name: str, body: S.GrantSecret, app: App = A, who: Principal = P):
    try:
        return app.secrets_store.set_grant(who.id, name, body.allow_in_cells)
    except KeyError:
        raise HTTPException(404, "no such secret") from None


@router.delete("/secrets/{name}", status_code=204)
def delete_secret(name: str, app: App = A, who: Principal = P):
    try:
        app.secrets_store.delete(who.id, name)
    except KeyError:
        raise HTTPException(404, "no such secret") from None


# ------------------------------------------------------------------ search, import / export
@router.post("/paper-envs/{paper_env_id}/find")
def find_text(paper_env_id: str, body: S.FindReq, app: App = A):
    return app.notebook.find(paper_env_id, body.pattern, regex=body.regex, case=body.case)


@router.post("/paper-envs/{paper_env_id}/replace")
def replace_text(paper_env_id: str, body: S.ReplaceReq, app: App = A):
    """Every change goes through apply_edit: versioned, attributed to ``author``, downstream cells go stale."""
    return app.notebook.replace_all(paper_env_id, body.pattern, body.replacement, body.author, regex=body.regex,
                                    case=body.case, cell_ids=body.cell_ids)


@router.post("/paper-envs/{paper_env_id}/import-ipynb", status_code=201)
def import_ipynb(paper_env_id: str, body: S.ImportReq, app: App = A):
    """Cells are added (never executed). ``!pip install`` lines are rewritten to comments and reported as packages."""
    return jsonable(app.ipynb.import_notebook(paper_env_id, body.notebook, install_packages=body.install_packages,
                                               author=body.author))


@router.get("/paper-envs/{paper_env_id}/export.ipynb")
def export_ipynb(paper_env_id: str, include_outputs: bool = True, app: App = A):
    nb = app.ipynb.export_notebook(paper_env_id, include_outputs=include_outputs)
    alias = app.paper_envs.get(paper_env_id).alias
    from fastapi.responses import JSONResponse
    return JSONResponse(nb, headers={"Content-Disposition": f'attachment; filename="{alias}.ipynb"'})


# ------------------------------------------------------------------ terminal (WebSocket)
@router.websocket("/paper-envs/{paper_env_id}/terminal")
async def terminal_ws(ws: WebSocket, paper_env_id: str, who: Principal = P):
    """Messages from the client: JSON {"type":"input","data":str} | {"type":"resize","rows":int,"cols":int}. Server
    sends raw (scrubbed) terminal text. Disabled unless the server enables it; goes through the pre-execute hook."""
    import json as _json
    app: App = ws.app.state.core
    await ws.accept()
    try:
        app.gate.check(Action(ActionKind.OPEN_TERMINAL, paper_env_id=paper_env_id, payload={"user": who.id}))
        term = app.terminal.open(paper_env_id)
    except ActionBlocked as b:
        await ws.send_text(f"\r\n[terminal blocked by policy ({b.decision.kind.value}): {b.decision.reason}]\r\n")
        await ws.close(code=1008)
        return
    except TerminalError as e:
        await ws.send_text(f"\r\n[{e}]\r\n")
        await ws.close(code=1008)
        return

    async def pump():
        while True:
            chunk = await asyncio.to_thread(term.read, 0.25)
            if chunk is None:
                return
            if chunk:
                await ws.send_text(chunk)

    task = asyncio.create_task(pump())
    try:
        while not task.done():
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            msg = _json.loads(raw)
            if msg.get("type") == "input":
                term.write(str(msg.get("data", "")))
            elif msg.get("type") == "resize":
                term.resize(int(msg.get("rows", 24)), int(msg.get("cols", 80)))
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        task.cancel()
        term.close()
        drift = await asyncio.to_thread(app.envs.detect_drift, paper_env_id)  # packages changed in the shell are recorded
        if drift:
            pass
