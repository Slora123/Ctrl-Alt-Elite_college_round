"""pipeline: papers, pipeline_runs, gates

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "papers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("paper_hash", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("size", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pdf", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_papers_paper_hash", "papers", ["paper_hash"])
    op.create_table(
        "pipeline_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("paper_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("node", sa.String(32), nullable=False),
        sa.Column("state", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pipeline_runs_paper_id", "pipeline_runs", ["paper_id"])
    op.create_table(
        "gates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("context", JSON, nullable=False),
        sa.Column("allowed_actions", JSON, nullable=False),
        sa.Column("action", sa.String(32), nullable=True),
        sa.Column("response", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_gates_run_id", "gates", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_gates_run_id", table_name="gates")
    op.drop_table("gates")
    op.drop_index("ix_pipeline_runs_paper_id", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
    op.drop_index("ix_papers_paper_hash", table_name="papers")
    op.drop_table("papers")
