from alembic import context
from sqlalchemy import engine_from_config, pool

from paper_env.repos.sqlite.tables import Base

config = context.config
target_metadata = Base.metadata


def _run(connection):
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    engine = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)
        conn.commit()
