"""Reversible trash; existing manuscripts and canon remain in place."""
from alembic import op
import sqlalchemy as sa

revision = "0003_trash"
down_revision = "0002_generation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("novels", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bible_entries", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_novels_deleted_at", "novels", ["deleted_at"])
    op.create_index("ix_bible_entries_deleted_at", "bible_entries", ["deleted_at"])


def downgrade():
    op.drop_index("ix_bible_entries_deleted_at", table_name="bible_entries")
    op.drop_index("ix_novels_deleted_at", table_name="novels")
    op.drop_column("bible_entries", "deleted_at")
    op.drop_column("novels", "deleted_at")
