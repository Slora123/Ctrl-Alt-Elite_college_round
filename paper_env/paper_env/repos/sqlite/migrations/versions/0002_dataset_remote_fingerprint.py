"""dataset remote_fingerprint

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("datasets") as b:
        b.add_column(sa.Column("remote_fingerprint", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("datasets") as b:
        b.drop_column("remote_fingerprint")
