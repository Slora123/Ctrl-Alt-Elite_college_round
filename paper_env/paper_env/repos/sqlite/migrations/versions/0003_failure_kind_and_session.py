"""cells.last_failure_kind, runs.kernel_session_id, paper_envs.dataset_ids, cell_runs.blocked_action_id

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cells") as b:
        b.add_column(sa.Column("last_failure_kind", sa.String(), nullable=True))
    with op.batch_alter_table("runs") as b:
        b.add_column(sa.Column("kernel_session_id", sa.String(), nullable=True))
    with op.batch_alter_table("paper_envs") as b:
        b.add_column(sa.Column("dataset_ids", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("cell_runs") as b:
        b.add_column(sa.Column("blocked_action_id", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cell_runs") as b:
        b.drop_column("blocked_action_id")
    with op.batch_alter_table("paper_envs") as b:
        b.drop_column("dataset_ids")
    with op.batch_alter_table("runs") as b:
        b.drop_column("kernel_session_id")
    with op.batch_alter_table("cells") as b:
        b.drop_column("last_failure_kind")
