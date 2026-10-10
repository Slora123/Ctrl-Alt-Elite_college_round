from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Response
from sqlalchemy import Engine, text

from .config import Settings
from .db.base import make_engine
from .logging import configure_logging

log = logging.getLogger("crisisgrid.api")


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    engine = engine or make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("api starting", extra={"env": settings.env})
        yield
        engine.dispose()

    app = FastAPI(title="CrisisGrid API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine

    @app.get("/health")
    def health(response: Response) -> dict[str, Any]:
        """Liveness plus DB reachability and the applied migration revision."""
        try:
            with engine.connect() as c:
                c.execute(text("select 1"))
                try:
                    rev = c.execute(text("select version_num from alembic_version")).scalar()
                except Exception:
                    rev = None  # migrations not applied
            return {"status": "ok", "db": "ok", "migration": rev}
        except Exception as e:  # noqa: BLE001
            log.warning("health check failed", extra={"error": str(e)})
            response.status_code = 503
            return {"status": "degraded", "db": "unreachable", "migration": None}

    return app


def __getattr__(name: str) -> Any:
    # lets `uvicorn crisisgrid_api.main:app` build the app lazily, without import-time side effects for tests
    if name == "app":
        return create_app()
    raise AttributeError(name)
