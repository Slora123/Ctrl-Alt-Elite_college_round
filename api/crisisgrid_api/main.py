from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Response
from sqlalchemy import Engine, text

from .config import Settings
from .container import AppContainer, build_container
from .db.base import make_engine
from .auth.routes import build_auth_router
from .logging import configure_logging
from .paper_env_mount import mount_paper_env
from .routes import build_router
from .routes_workers import build_worker_router

log = logging.getLogger("crisisgrid.api")


async def _reap_forever(container: AppContainer, interval: float) -> None:
    """Jobs whose worker went silent are infrastructure failures: requeue once, then fail them (JobService.reap)."""
    while True:
        await asyncio.sleep(interval)
        try:
            reaped = await asyncio.to_thread(container.jobs.reap)
            if reaped:
                log.warning("reaped jobs", extra={"count": len(reaped)})
        except Exception:  # noqa: BLE001  (a failed sweep must never kill the API; the next one retries)
            log.exception("reaper sweep failed")


def create_app(settings: Settings | None = None, engine: Engine | None = None, container: AppContainer | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    engine = engine or make_engine(settings.database_url)
    container = container or build_container(engine, settings)

    pe_holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("api starting", extra={"env": settings.env, "auth": settings.auth_enabled})
        reaper = asyncio.create_task(_reap_forever(container, settings.reaper_interval_s)) if settings.reaper_interval_s > 0 else None
        pe = pe_holder.get("core")
        if pe is not None:  # mounted apps get no lifespan events, so we start/stop paper_env ourselves
            await asyncio.to_thread(pe.startup)
        yield
        if reaper is not None:
            reaper.cancel()
        if pe is not None:
            await asyncio.to_thread(pe.shutdown)
        engine.dispose()

    app = FastAPI(title="CrisisGrid API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.container = container
    app.include_router(build_auth_router(container.auth, container.auth_deps, settings))
    app.include_router(build_router(container))
    app.include_router(build_worker_router(container, settings))

    pe_holder["core"] = mount_paper_env(app, container, settings)
    app.state.paper_env = pe_holder["core"]

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
            return {"status": "ok", "db": "ok", "migration": rev, "auth": settings.auth_enabled,
                    "paper_env": pe_holder["core"] is not None}
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
