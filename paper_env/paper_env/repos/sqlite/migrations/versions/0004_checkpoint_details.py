"""checkpoint resume metadata

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_COLS = [("cell_position", sa.Integer(), "0"), ("seed", sa.Integer(), "0"), ("notebook_version", sa.Integer(), "0"),
         ("cell_versions", sa.JSON(), "{}"), ("file_hashes", sa.JSON(), "{}"), ("notes", sa.JSON(), "[]")]


def upgrade() -> None:
    with op.batch_alter_table("checkpoints") as b:
        for name, typ, default in _COLS:
            b.add_column(sa.Column(name, typ, nullable=False, server_default=default))


def downgrade() -> None:
    with op.batch_alter_table("checkpoints") as b:
        for name, _, _ in reversed(_COLS):
            b.drop_column(name)
