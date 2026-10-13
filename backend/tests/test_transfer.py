"""Real transfer tests; PostgreSQL uses an isolated disposable schema."""
from datetime import datetime, timezone
import hashlib
import os
from uuid import uuid4
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.auth import hash_password, verify_password
from app.cli import migrate
from app.config import Settings
from app.database import Base, make_engine
from app.models import AuthorDecision, BibleEntry, Chapter, Novel, RejectRule, Revision, User, WebSession
from app.generation.models import GenerationJob, PromptVersion, UsageLedger
from app.services import payload_hash
from app.transfer import TransferError, readonly_source_engine, transfer_database

PASSWORD = "portable-author-password"
MOMENT = datetime(2025, 6, 7, 9, 10, tzinfo=timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@pytest.fixture
def source_database(tmp_path):
    path = tmp_path / "роман # 1.db"
    settings = Settings(database_url=f"sqlite:///{path.as_posix()}", openai_api_key="")
    migrate(settings)
    ids = {name: str(uuid4()) for name in (
        "user", "novel", "chapter", "chapter2", "old_revision", "new_revision",
        "old_character", "new_character", "future", "decision", "rule",
        "queued", "running", "ready", "prompt", "ledger",
    )}
    engine = make_engine(settings.database_url)
    old_text = "Анна получила письмо и сохранила его до следующего утра."
    new_text = "Марина получила письмо. Автор вручную исправила её имя."
    with engine.begin() as db:
        db.execute(User.__table__.insert(), {
            "id": ids["user"], "email": "portable@example.test", "password_hash": hash_password(PASSWORD),
            "created_at": MOMENT,
        })
        db.execute(Novel.__table__.insert(), {
            "id": ids["novel"], "owner_id": ids["user"], "title": "Наследство",
            "genre": "романтика", "concept": "Наследство требует фиктивного брака.",
            "version": 9, "canon_version": 42, "created_at": MOMENT, "updated_at": MOMENT,
            "settings": {"target_min_chars": 6000, "target_max_chars": 9000,
                         "max_review_passes": 3, "job_budget_usd": 0.6, "pace": "balanced"},
        })
        for name, number in (("chapter", 1), ("chapter2", 2)):
            db.execute(Chapter.__table__.insert(), {
                "id": ids[name], "novel_id": ids["novel"], "number": number,
                "title": f"Глава {number}", "created_at": MOMENT, "updated_at": MOMENT,
            })
        db.execute(Revision.__table__.insert(), {
            "id": ids["new_revision"], "chapter_id": ids["chapter"], "parent_revision_id": None,
            "text": new_text, "source": "manual", "mutation_id": "source-mutation-new",
            "payload_hash": payload_hash(ids["old_revision"], new_text), "created_at": MOMENT,
            "deleted_fragments": ["Удалённая автором реплика."],
        })
        db.execute(Revision.__table__.insert(), {
            "id": ids["old_revision"], "chapter_id": ids["chapter"], "parent_revision_id": None,
            "text": old_text, "source": "manual", "mutation_id": "source-mutation-old",
            "payload_hash": payload_hash(None, old_text), "created_at": MOMENT, "deleted_fragments": [],
        })
        db.execute(Revision.__table__.update().where(Revision.id == ids["new_revision"])
                   .values(parent_revision_id=ids["old_revision"]))
        db.execute(Chapter.__table__.update().where(Chapter.id == ids["chapter"]).values(
            current_revision_id=ids["new_revision"], approved_revision_id=ids["old_revision"],
            status="DRAFT", updated_at=MOMENT))
        db.execute(BibleEntry.__table__.insert(), {
            "id": ids["new_character"], "novel_id": ids["novel"], "kind": "character",
            "title": "Марина", "content": "Героиню зовут Марина.", "status": "CANON",
            "version": 3, "active": True, "details": {"name": "Марина"}, "created_at": MOMENT,
        })
        db.execute(BibleEntry.__table__.insert(), {
            "id": ids["old_character"], "novel_id": ids["novel"], "kind": "character",
            "title": "Анна", "content": "Прежнее имя героини.", "status": "CANON",
            "version": 4, "active": False, "details": {"name": "Анна"}, "created_at": MOMENT,
        })
        db.execute(BibleEntry.__table__.update().where(BibleEntry.id == ids["new_character"])
                   .values(supersedes_id=ids["old_character"]))
        db.execute(BibleEntry.__table__.insert(), {
            "id": ids["future"], "novel_id": ids["novel"], "kind": "future_vault",
            "title": "Будущий секрет", "content": "Незнакомец является наследником.",
            "status": "FUTURE_CANON", "available_from_chapter": 15,
            "details": {"reveal_phrases": ["является наследником"]}, "created_at": MOMENT,
        })
        db.execute(AuthorDecision.__table__.insert(), {
            "id": ids["decision"], "novel_id": ids["novel"], "user_id": ids["user"],
            "bible_entry_id": ids["new_character"], "action": "approve",
            "reason": "Автор изменила имя.", "payload": {"old_name": "Анна", "new_name": "Марина"},
            "created_at": MOMENT,
        })
        db.execute(RejectRule.__table__.insert(), {
            "id": ids["rule"], "novel_id": ids["novel"], "phrase": "Анна",
            "description": "Запрещено возвращать прежнее имя.", "scope": ids["chapter"],
            "source_decision_id": ids["decision"], "severity": "critical",
            "repeat_count": 2, "status": "ACTIVE", "created_at": MOMENT,
        })
        for name, chapter_name, status in (
            ("queued", "chapter", "queued"), ("running", "chapter2", "running"),
            ("ready", "chapter", "ready"),
        ):
            db.execute(GenerationJob.__table__.insert(), {
                "id": ids[name], "novel_id": ids["novel"], "chapter_id": ids[chapter_name],
                "active_chapter_id": ids[chapter_name] if status != "ready" else None,
                "base_revision_id": ids["new_revision"] if chapter_name == "chapter" else None,
                "canon_version": 42, "kind": "write", "status": status,
                "stage": "writer" if status == "running" else status,
                "idempotency_key": "source-job-" + name, "request_hash": "a" * 64,
                "instructions": "Сохранить авторский сюжет.", "snapshot": {"canon_version": 42},
                "artifacts": {"draft": "Сохранённый кандидат " + name},
                "usage": {"input_tokens": 50, "cost_usd": 0.1},
                "lease_owner": str(uuid4()) if status == "running" else None,
                "lease_until": 9999999999.0 if status == "running" else None,
                "attempts": 2 if status == "running" else 0,
                "error": "Прежнее сообщение" if status == "running" else None,
            })
        db.execute(PromptVersion.__table__.insert(), {
            "id": ids["prompt"], "novel_id": ids["novel"], "agent": "writer",
            "version": 4, "text": "Пиши плавную русскую прозу.",
        })
        db.execute(UsageLedger.__table__.insert(), {
            "id": ids["ledger"], "user_id": ids["user"], "day": "2026-10-13", "cost_usd": 0.43,
        })
        db.execute(WebSession.__table__.insert(), {
            "token_hash": "b" * 64, "user_id": ids["user"], "csrf_token": "old-csrf",
            "expires_at": 9999999999.0, "created_at": MOMENT,
        })
    engine.dispose()
    return path, settings, ids, old_text, new_text


@pytest.fixture(params=["sqlite", "postgresql"])
def target_settings(request, tmp_path):
    if request.param == "sqlite":
        settings = Settings(database_url=f"sqlite:///{(tmp_path / 'target.db').as_posix()}", openai_api_key="")
        migrate(settings)
        yield settings
        return
    test_url = os.environ.get("NOVEL_TRANSFER_TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("NOVEL_TRANSFER_TEST_DATABASE_URL not configured")
    parsed = make_url(test_url)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("NOVEL_TRANSFER_TEST_DATABASE_URL must designate PostgreSQL")
    schema = "novel_transfer_" + uuid4().hex
    admin = make_engine(test_url)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    options = (parsed.query.get("options", "") + f" -csearch_path={schema}").strip()
    scoped_url = parsed.update_query_dict({"options": options}).render_as_string(hide_password=False)
    settings = Settings(database_url=scoped_url, openai_api_key="")
    try:
        migrate(settings)
        yield settings
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def assert_empty(settings):
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as db:
            for table in Base.metadata.tables.values():
                assert db.scalar(select(func.count()).select_from(table)) == 0, table.name
    finally:
        engine.dispose()


def test_transfer_preserves_identity_canon_history_and_cancels_work(source_database, target_settings):
    path, _, ids, old_text, new_text = source_database
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    report = transfer_database(path, target_settings)
    assert report["cancelled_jobs"] == 2 and report["skipped_sessions"] == 1
    assert report["copied"]["revisions"] == 2 and report["copied"]["generation_jobs"] == 3
    assert hashlib.sha256(path.read_bytes()).hexdigest() == fingerprint
    engine = make_engine(target_settings.database_url)
    try:
        with Session(engine) as db:
            author = db.get(User, ids["user"])
            assert author.email == "portable@example.test" and verify_password(PASSWORD, author.password_hash)
            work = db.get(Novel, ids["novel"])
            assert work.version == 9 and work.canon_version == 42
            assert work.settings["job_budget_usd"] == 0.6 and work.owner_id == ids["user"]
            page = db.get(Chapter, ids["chapter"])
            assert page.current_revision_id == ids["new_revision"]
            assert page.approved_revision_id == ids["old_revision"] and utc(page.updated_at) == MOMENT
            old = db.get(Revision, ids["old_revision"])
            new = db.get(Revision, ids["new_revision"])
            assert old.text == old_text and new.text == new_text and new.parent_revision_id == old.id
            assert new.deleted_fragments == ["Удалённая автором реплика."]
            assert new.payload_hash == payload_hash(old.id, new_text)
            old_character = db.get(BibleEntry, ids["old_character"])
            new_character = db.get(BibleEntry, ids["new_character"])
            assert not old_character.active and new_character.active
            assert new_character.supersedes_id == old_character.id and new_character.status == "CANON"
            assert db.get(BibleEntry, ids["future"]).available_from_chapter == 15
            rule = db.get(RejectRule, ids["rule"])
            assert rule.phrase == "Анна" and rule.repeat_count == 2 and rule.source_decision_id == ids["decision"]
            assert db.get(AuthorDecision, ids["decision"]).payload["new_name"] == "Марина"
            assert db.get(PromptVersion, ids["prompt"]).version == 4
            assert db.get(UsageLedger, ids["ledger"]).cost_usd == pytest.approx(0.43)
            assert db.scalar(select(func.count()).select_from(WebSession)) == 0
            for name in ("queued", "running"):
                job = db.get(GenerationJob, ids[name])
                assert job.status == "cancelled" and job.cancel_requested
                assert job.active_chapter_id is None and job.lease_owner is None and job.lease_until is None
                assert job.artifacts["draft"] == "Сохранённый кандидат " + name
                assert job.snapshot["canon_version"] == 42 and job.usage["cost_usd"] == 0.1
            assert "Прежнее сообщение" in db.get(GenerationJob, ids["running"]).error
            assert db.get(GenerationJob, ids["ready"]).status == "ready"
    finally:
        engine.dispose()
    source_engine = readonly_source_engine(path)
    try:
        with source_engine.connect() as source:
            assert source.execute(select(GenerationJob.status).where(
                GenerationJob.id == ids["running"])).scalar_one() == "running"
            assert source.scalar(select(func.count()).select_from(WebSession)) == 1
    finally:
        source_engine.dispose()


def test_nonempty_target_is_refused_without_overwriting(source_database, target_settings):
    path, _, _, _, _ = source_database
    engine = make_engine(target_settings.database_url)
    with engine.begin() as db:
        db.execute(User.__table__.insert(), {
            "id": str(uuid4()), "email": "existing@example.test", "password_hash": "preserve-this",
        })
    with pytest.raises(TransferError, match="не пуста"):
        transfer_database(path, target_settings)
    with engine.connect() as db:
        assert db.execute(select(User.email)).scalars().all() == ["existing@example.test"]
        assert db.scalar(select(func.count()).select_from(Novel)) == 0
    engine.dispose()


def test_even_anonymous_target_sessions_block_import(source_database, target_settings):
    path, _, _, _, _ = source_database
    engine = make_engine(target_settings.database_url)
    with engine.begin() as db:
        db.execute(WebSession.__table__.insert(), {
            "token_hash": "c" * 64, "user_id": None, "csrf_token": "csrf", "expires_at": 9999999999.0,
        })
    with pytest.raises(TransferError, match="не пуста"):
        transfer_database(path, target_settings)
    with engine.connect() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert db.scalar(select(func.count()).select_from(WebSession)) == 1
    engine.dispose()


@pytest.mark.parametrize("wrong_side", ["source", "target"])
def test_wrong_migration_version_leaves_no_partial_import(source_database, target_settings, wrong_side):
    path, source_settings, _, _, _ = source_database
    altered = source_settings if wrong_side == "source" else target_settings
    engine = make_engine(altered.database_url)
    with engine.begin() as db:
        db.execute(text("UPDATE alembic_version SET version_num='obsolete_revision'"))
    engine.dispose()
    with pytest.raises(TransferError, match="версия Alembic"):
        transfer_database(path, target_settings)
    assert_empty(target_settings)


def test_insert_failure_rolls_back_earlier_rows_and_hides_private_data(source_database, target_settings):
    path, _, _, _, _ = source_database
    engine = make_engine(target_settings.database_url)
    with engine.begin() as db:
        if engine.dialect.name == "sqlite":
            db.exec_driver_sql(
                "CREATE TRIGGER reject_transfer BEFORE INSERT ON novels "
                "BEGIN SELECT RAISE(ABORT, 'reject imported novel'); END"
            )
        else:
            db.exec_driver_sql("ALTER TABLE novels ADD CONSTRAINT reject_transfer CHECK (false)")
    engine.dispose()
    with pytest.raises(TransferError) as caught:
        transfer_database(path, target_settings)
    assert PASSWORD not in str(caught.value) and "portable@example.test" not in str(caught.value)
    assert_empty(target_settings)


def test_same_file_target_is_refused_without_modifying_source(source_database):
    path, settings, _, _, _ = source_database
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(TransferError, match="одним файлом"):
        transfer_database(path, settings)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == fingerprint


def test_in_memory_target_cannot_report_ephemeral_success(source_database):
    path, _, _, _, _ = source_database
    settings = Settings(database_url="sqlite:///:memory:", openai_api_key="")
    with pytest.raises(TransferError, match="постоянная база"):
        transfer_database(path, settings)
