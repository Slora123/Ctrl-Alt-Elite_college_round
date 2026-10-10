"""Engine/session plumbing and the repository base class. SQLAlchemy is confined to this package."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite") and (":memory:" in url or url in ("sqlite://", "sqlite:///")):
        # one shared connection so an in-memory DB survives across sessions (tests)
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Unit of work: commit on success, roll back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class SqlRepository:
    """Base for SQL repositories: holds the session factory and gives each call its own unit of work."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def _scope(self) -> AbstractContextManager[Session]:
        return session_scope(self._factory)
