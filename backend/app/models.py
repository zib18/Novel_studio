from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


def uid():
    return str(uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebSession(Base):
    __tablename__ = "web_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Novel(Base):
    __tablename__ = "novels"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    genre: Mapped[str] = mapped_column(String(240), default="")
    concept: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    canon_version: Mapped[int] = mapped_column(Integer, default=1)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("novel_id", "number", name="uq_chapter_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240), default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    current_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("revisions.id", use_alter=True, name="fk_current_revision"), nullable=True)
    approved_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("revisions.id", use_alter=True, name="fk_approved_revision"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (UniqueConstraint("chapter_id", "mutation_id", name="uq_revision_mutation"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    parent_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(40))
    mutation_id: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    deleted_fragments: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BibleEntry(Base):
    __tablename__ = "bible_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(240))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="PROPOSAL")
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("bible_entries.id"), nullable=True)
    available_from_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthorDecision(Base):
    __tablename__ = "author_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    bible_entry_id: Mapped[str | None] = mapped_column(ForeignKey("bible_entries.id"), nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RejectRule(Base):
    __tablename__ = "reject_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id"), index=True)
    phrase: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text)
    source_decision_id: Mapped[str] = mapped_column(ForeignKey("author_decisions.id"))
    scope: Mapped[str | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="critical")
    repeat_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
