"""experiment graph versions

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "graph_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("paper_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(16), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("graph", JSON, nullable=False),
        sa.Column("cells", JSON, nullable=False),
        sa.Column("run_ids", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_graph_versions_paper_id", "graph_versions", ["paper_id"])
    op.create_index("ix_graph_versions_paper_version", "graph_versions", ["paper_id", "version"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_graph_versions_paper_version", table_name="graph_versions")
    op.drop_index("ix_graph_versions_paper_id", table_name="graph_versions")
    op.drop_table("graph_versions")
