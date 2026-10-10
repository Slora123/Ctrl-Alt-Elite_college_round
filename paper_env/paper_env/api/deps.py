from __future__ import annotations

import dataclasses
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..container import App
from ..env.manager import InvalidSpec
from ..repos import Conflict, NotFound
from ..services.checkpoints import CheckpointError
from ..services.notebook import CellBusy
from ..services.runs import EnvNotReady, RunInProgress
from ..datasets import DatasetNotAvailable


def get_app(request: Request) -> App:
    return request.app.state.core


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
                        (RunInProgress, 409), (EnvNotReady, 409), (CheckpointError, 409),
                        (DatasetNotAvailable, 409), (InvalidSpec, 422), (ValueError, 422)):
        api.add_exception_handler(exc, mk(status))
