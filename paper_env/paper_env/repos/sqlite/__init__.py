"""SQLite persistence. The only package allowed to import sqlalchemy."""
from __future__ import annotations

from .db import make_engine, make_session_factory, migrate
from .repos import SqliteUnitOfWork


def sqlite_uow_factory(url: str, run_migrations: bool = True):
    """Build a UnitOfWorkFactory backed by SQLite (e.g. ``sqlite:///paper_env.db``)."""
    engine = make_engine(url)
    if run_migrations:
        migrate(engine)
    sf = make_session_factory(engine)
    factory = lambda: SqliteUnitOfWork(sf)  # noqa: E731
    factory.engine = engine  # type: ignore[attr-defined]
    return factory


__all__ = ["sqlite_uow_factory", "SqliteUnitOfWork", "migrate", "make_engine"]
