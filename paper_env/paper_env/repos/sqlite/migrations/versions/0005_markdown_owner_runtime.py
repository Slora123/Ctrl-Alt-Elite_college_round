"""markdown cells, ownership, runtime profile, session limits

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cells") as b:
        b.add_column(sa.Column("cell_type", sa.String(), nullable=False, server_default="code"))
    with op.batch_alter_table("paper_envs") as b:
        b.add_column(sa.Column("owner_id", sa.String(), nullable=True))
        b.add_column(sa.Column("runtime", sa.String(), nullable=False, server_default="local-cpu"))
        b.add_column(sa.Column("limits", sa.JSON(), nullable=False,
                               server_default='{"idle_timeout_s": 1800, "max_session_s": 43200, "max_cost": null}'))
        b.create_index("ix_paper_envs_owner_id", ["owner_id"])


def downgrade() -> None:
    with op.batch_alter_table("paper_envs") as b:
        b.drop_index("ix_paper_envs_owner_id")
        b.drop_column("limits")
        b.drop_column("runtime")
        b.drop_column("owner_id")
    with op.batch_alter_table("cells") as b:
        b.drop_column("cell_type")
