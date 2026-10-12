"""Frozen schema for durable generation jobs, prompt versions and cost ledger."""
from alembic import op
import sqlalchemy as sa

revision = "0002_generation"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("generation_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id"), nullable=False),
        sa.Column("chapter_id", sa.String(36), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("active_chapter_id", sa.String(36), unique=True),
        sa.Column("base_revision_id", sa.String(36)),
        sa.Column("canon_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("review_passes", sa.Integer(), nullable=False),
        sa.Column("max_review_passes", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("lease_owner", sa.String(36)),
        sa.Column("lease_until", sa.Float()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("novel_id", "idempotency_key", name="uq_job_idempotency"))
    for column in ("novel_id", "chapter_id", "status"):
        op.create_index("ix_generation_jobs_" + column, "generation_jobs", [column])
    op.create_table("prompt_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id"), nullable=False),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("novel_id", "agent", "version", name="uq_prompt_version"))
    op.create_index("ix_prompt_versions_novel_id", "prompt_versions", ["novel_id"])
    op.create_table("usage_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.UniqueConstraint("user_id", "day", name="uq_user_day_usage"))
    op.create_index("ix_usage_ledger_user_id", "usage_ledger", ["user_id"])


def downgrade():
    op.drop_table("usage_ledger")
    op.drop_table("prompt_versions")
    op.drop_table("generation_jobs")
