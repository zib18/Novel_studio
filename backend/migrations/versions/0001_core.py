"""Durable author-owned novels, immutable revisions and author decisions.

This migration is a fixed schema snapshot; it does not import live application models.
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def pk():
    return sa.Column("id", sa.String(36), primary_key=True)


def created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def novel_ref():
    return sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id"), nullable=False)


def upgrade():
    op.create_table("users", pk(),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False), created())
    op.create_table("web_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("csrf_token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False), created())
    op.create_table("novels", pk(),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("genre", sa.String(240), nullable=False),
        sa.Column("concept", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("canon_version", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False), created(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    chapter_args = []
    if op.get_bind().dialect.name == "sqlite":
        chapter_args = [
            sa.ForeignKeyConstraint(["current_revision_id"], ["revisions.id"], name="fk_current_revision"),
            sa.ForeignKeyConstraint(["approved_revision_id"], ["revisions.id"], name="fk_approved_revision"),
        ]
    op.create_table("chapters", pk(), novel_ref(),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_revision_id", sa.String(36)),
        sa.Column("approved_revision_id", sa.String(36)), created(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("novel_id", "number", name="uq_chapter_number"), *chapter_args)
    op.create_table("revisions", pk(),
        sa.Column("chapter_id", sa.String(36), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("parent_revision_id", sa.String(36), sa.ForeignKey("revisions.id")),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("mutation_id", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("deleted_fragments", sa.JSON(), nullable=False), created(),
        sa.UniqueConstraint("chapter_id", "mutation_id", name="uq_revision_mutation"))
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key("fk_current_revision", "chapters", "revisions", ["current_revision_id"], ["id"])
        op.create_foreign_key("fk_approved_revision", "chapters", "revisions", ["approved_revision_id"], ["id"])
    op.create_table("bible_entries", pk(), novel_ref(),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("supersedes_id", sa.String(36), sa.ForeignKey("bible_entries.id")),
        sa.Column("available_from_chapter", sa.Integer()),
        sa.Column("details", sa.JSON(), nullable=False), created())
    op.create_table("author_decisions", pk(), novel_ref(),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("bible_entry_id", sa.String(36), sa.ForeignKey("bible_entries.id")),
        sa.Column("chapter_id", sa.String(36), sa.ForeignKey("chapters.id")),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False), created())
    op.create_table("reject_rules", pk(), novel_ref(),
        sa.Column("phrase", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_decision_id", sa.String(36), sa.ForeignKey("author_decisions.id"), nullable=False),
        sa.Column("scope", sa.String(36), sa.ForeignKey("chapters.id")),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("repeat_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), created())
    for table, column in (
        ("web_sessions", "user_id"), ("novels", "owner_id"), ("chapters", "novel_id"),
        ("revisions", "chapter_id"), ("bible_entries", "novel_id"),
        ("author_decisions", "novel_id"), ("reject_rules", "novel_id"),
    ):
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_current_revision", "chapters", type_="foreignkey")
        op.drop_constraint("fk_approved_revision", "chapters", type_="foreignkey")
    else:
        op.execute("UPDATE chapters SET current_revision_id = NULL, approved_revision_id = NULL")
    for table in ("reject_rules", "author_decisions", "bible_entries", "revisions",
                  "chapters", "novels", "web_sessions", "users"):
        op.drop_table(table)
