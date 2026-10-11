"""papers.pages: page count, recorded at upload so the library does not have to re-read every PDF

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("papers") as b:
        b.add_column(sa.Column("pages", sa.Integer, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("papers") as b:
        b.drop_column("pages")
