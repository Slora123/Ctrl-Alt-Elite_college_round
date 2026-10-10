from __future__ import annotations

from alembic import context

from crisisgrid_api.config import Settings
from crisisgrid_api.db import tables  # noqa: F401  (register tables on Base.metadata)
from crisisgrid_api.db.base import Base, make_engine

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # tests/CLI may pass sqlalchemy.url explicitly; otherwise use CG_DATABASE_URL
    return config.get_main_option("sqlalchemy.url") or Settings.from_env().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
