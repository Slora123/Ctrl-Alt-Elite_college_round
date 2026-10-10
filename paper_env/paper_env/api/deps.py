from __future__ import annotations

import dataclasses
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocketException
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

from ..auth import ANONYMOUS, Principal, current_principal, token_from
from ..container import App
from ..env.manager import InvalidSpec
from ..repos import Conflict, NotFound
from ..services.checkpoints import CheckpointError
from ..services.files import TooLarge, UnsafePath
from ..services.notebook import CellBusy
from ..services.runs import EnvNotReady, RunInProgress
from ..kernel.runner import SessionError
from ..backends import BackendError
from ..datasets import DatasetNotAvailable
from ..backends.registry import RuntimeUnavailable
from ..security.store import SecretError
from ..services.ipynb import NotebookFormatError


def get_app(conn: HTTPConnection) -> App:
    return conn.app.state.core


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def principal_of(conn: HTTPConnection) -> Principal:
    core: App = conn.app.state.core
    if core.authenticator is None:
        if core.settings.require_auth:
            raise HTTPException(401, "authentication is required but no authenticator is configured")
        return ANONYMOUS  # standalone development on loopback
    token = token_from(conn.headers.get("authorization"), conn.query_params.get("token"))
    p = core.authenticator.authenticate(token) if token else None
    if p is None:
        raise HTTPException(401, "missing or invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    return p


def env_id_for(core: App, params: dict[str, str]) -> str | None:
    """The paper environment a request touches, derived from its path parameters."""
    if "paper_env_id" in params:
        return params["paper_env_id"]
    with core.uow() as u:
        if "cell_id" in params:
            return u.cells.get_cell(params["cell_id"]).paper_env_id
        if "version_id" in params:
            return u.cells.get_cell(u.cells.get_version(params["version_id"]).cell_id).paper_env_id
        if "run_id" in params:
            return u.runs.get_run(params["run_id"]).paper_env_id
        if "checkpoint_id" in params:
            return u.checkpoints.get(params["checkpoint_id"]).paper_env_id
        if "action_id" in params:
            return u.blocked.get(params["action_id"]).paper_env_id
    return None


def can_see(p: Principal, owner_id: str | None) -> bool:
    return p.is_admin or owner_id is None or owner_id == p.id


def visible_env_ids(core: App, p: Principal) -> set[str] | None:
    """None means 'all' (admin)."""
    if p.is_admin:
        return None
    return {e.id for e in core.paper_envs.list() if can_see(p, e.owner_id)}


def check_env_access(core: App, p: Principal, env_id: str) -> None:
    """403 unless ``p`` may touch the paper environment (404 if it does not exist). For ids that arrive in a request BODY,
    which the router-level path-parameter check cannot see."""
    if not can_see(p, core.paper_envs.get(env_id).owner_id):
        raise HTTPException(403, "this paper environment belongs to another user")


async def authorize(conn: HTTPConnection) -> Principal:
    """Router-level dependency for every route: authentication, role (viewers are read-only) and ownership of the
    paper environment being touched. Sets the principal for the request (used for per-user secrets)."""
    core: App = conn.app.state.core
    is_ws = conn.scope["type"] == "websocket"
    try:
        p = principal_of(conn)
        if not is_ws and conn.scope["method"] not in SAFE_METHODS and not p.can_write:
            raise HTTPException(403, "your role is read-only")
        if is_ws and not p.can_write and conn.scope["path"].endswith("/terminal"):
            raise HTTPException(403, "your role cannot open a terminal")
        env_id = env_id_for(core, dict(conn.path_params))
        if env_id is not None:
            owner = core.paper_envs.get(env_id).owner_id  # NotFound -> 404 via the registered handler
            if not can_see(p, owner):
                raise HTTPException(403, "this paper environment belongs to another user")
    except HTTPException as e:
        if is_ws:
            raise WebSocketException(code=1008, reason=str(e.detail)) from e
        raise
    current_principal.set(p)
    conn.state.principal = p
    return p


def jsonable(obj: Any) -> Any:
    """Dataclasses (service results) -> plain JSON-able data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def install_error_handlers(api: FastAPI) -> None:
    def mk(status: int):
        async def handler(request: Request, exc: Exception):
            return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)
        return handler

    for exc, status in ((NotFound, 404), (FileNotFoundError, 404), (Conflict, 409), (CellBusy, 409),
                        (RunInProgress, 409), (SessionError, 409), (BackendError, 409), (EnvNotReady, 409), (CheckpointError, 409),
                        (DatasetNotAvailable, 409), (TooLarge, 413), (RuntimeUnavailable, 409), (SecretError, 422), (NotebookFormatError, 422), (UnsafePath, 422), (InvalidSpec, 422), (ValueError, 422)):
        api.add_exception_handler(exc, mk(status))
