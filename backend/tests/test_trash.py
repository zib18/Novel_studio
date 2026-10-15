"""Reversible deletion, author isolation, migration and durable manual text."""
from pathlib import Path
from uuid import uuid4
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, insert, select
from app.auth import hash_password
from app.cli import backup, migrate
from app.config import Settings
from app.database import make_engine
from app.main import create_app
from app.models import AuthorDecision, Chapter, Novel, User, utcnow
from app.generation.context import make_snapshot
from app.generation.models import GenerationJob
from test_core import EMAIL, ORIGIN, P, PASSWORD, chapter, decide, idea, novel, save, sign_in, studio


def current(client, work):
    return client.get(P + f"/novels/{work['id']}").json()


def novel_action(client, work, action, expected=200):
    response = client.post(P + f"/novels/{work['id']}/{action}", json={
        "base_version": work["version"], "expected_canon_version": work["canon_version"]})
    assert response.status_code == expected, response.text
    return response.json()


def entry_action(client, entry, work, action, expected=200):
    response = client.post(P + f"/bible/{entry['id']}/{action}", json={
        "base_version": entry["version"], "expected_canon_version": work["canon_version"]})
    assert response.status_code == expected, response.text
    return response.json()


def seed_job(app, work, page, status="queued"):
    with app.state.session_factory() as db:
        row = GenerationJob(
            novel_id=work["id"], chapter_id=page["id"], active_chapter_id=page["id"],
            base_revision_id=page["current_revision"]["id"], canon_version=work["canon_version"],
            kind="write", status=status, stage="writer",
            idempotency_key=str(uuid4()), request_hash="a" * 64, snapshot={"bible": []},
            artifacts={"draft_text": "Сохранённый вариант ИИ."},
            usage={"budget_usd": 1, "cost_usd": 0.01, "calls": []},
            lease_owner="worker-test" if status == "running" else None,
            lease_until=9999999999 if status == "running" else None)
        db.add(row)
        db.commit()
        return row.id


def test_novel_trash_restore_preserves_manual_revisions_canon_and_identity(studio):
    app, client, _ = studio
    work = novel(client)
    page = chapter(client, work["id"])
    first = save(client, page["id"], page["current_revision"]["id"], "Первая авторская версия.").json()
    latest = save(client, page["id"], first["current_revision"]["id"], "Последняя ручная правка.").json()
    fact = idea(client, work["id"])
    assert decide(client, fact, current(client, work)["canon_version"]).status_code == 200
    before = current(client, work)
    removed = novel_action(client, before, "trash")
    assert removed["deleted_at"] and removed["version"] == before["version"] + 1
    assert removed["canon_version"] == before["canon_version"] + 1
    assert client.get(P + "/novels").json()["items"] == []
    assert {n["id"] for n in client.get(P + "/trash/novels").json()["items"]} == {work["id"]}
    for path in (f"/novels/{work['id']}", f"/novels/{work['id']}/bible",
                 f"/novels/{work['id']}/chapters", f"/chapters/{page['id']}",
                 f"/revisions/{first['current_revision']['id']}",
                 f"/novels/{work['id']}/export?format=txt"):
        assert client.get(P + path).status_code == 404, path
    assert save(client, page["id"], latest["current_revision"]["id"], "Правка удалённого романа.").status_code == 404
    restored = novel_action(client, removed, "restore")
    assert restored["id"] == work["id"] and restored["deleted_at"] is None
    assert restored["version"] == removed["version"] + 1
    assert client.get(P + "/trash/novels").json()["total"] == 0
    actual = client.get(P + f"/chapters/{page['id']}").json()
    assert actual["current_revision"]["id"] == latest["current_revision"]["id"]
    assert actual["current_revision"]["text"] == "Последняя ручная правка."
    assert client.get(P + f"/chapters/{page['id']}/revisions").json()["total"] == 3
    rows = client.get(P + f"/novels/{work['id']}/bible").json()["items"]
    assert next(row for row in rows if row["id"] == fact["id"])["status"] == "CANON"
    with app.state.session_factory() as db:
        actions = db.scalars(select(AuthorDecision.action).where(AuthorDecision.novel_id == work["id"])).all()
        assert "trash_novel" in actions and "restore_novel" in actions


@pytest.mark.parametrize("status", ["IDEA", "PROPOSAL", "UNDEFINED"])
def test_idea_trash_excludes_snapshot_and_restore_does_not_approve(studio, status):
    app, client, _ = studio
    work = novel(client)
    page = chapter(client, work["id"])
    entry = idea(client, work["id"], kind="idea", status=status, title="Вариант наследства")
    before = current(client, work)
    result = entry_action(client, entry, before, "trash")
    removed, changed = result["entry"], result["novel"]
    assert removed["deleted_at"] and removed["status"] == status
    assert changed["canon_version"] == before["canon_version"] + 1
    assert client.get(P + f"/novels/{work['id']}/bible").json()["total"] == 0
    assert {r["id"] for r in client.get(P + f"/novels/{work['id']}/trash/ideas").json()["items"]} == {entry["id"]}
    assert decide(client, removed, changed["canon_version"]).status_code == 404
    with app.state.session_factory() as db:
        snapshot = make_snapshot(db, db.get(Novel, work["id"]), db.get(Chapter, page["id"]), "", 40000)
        assert entry["id"] not in {r["id"] for r in snapshot["bible"]}
    restored = entry_action(client, removed, changed, "restore")
    assert restored["entry"]["id"] == entry["id"]
    assert restored["entry"]["deleted_at"] is None and restored["entry"]["status"] == status
    assert restored["entry"]["version"] == removed["version"] + 1
    assert client.get(P + f"/novels/{work['id']}/trash/ideas").json()["total"] == 0


@pytest.mark.parametrize("approved_status", ["CANON", "FACT", "FUTURE_CANON", "REJECTED"])
def test_idea_trash_cannot_erase_approved_facts_or_author_rejections(studio, approved_status):
    _, client, _ = studio
    work = novel(client)
    entry = idea(client, work["id"], available_from_chapter=15)
    result = decide(client, entry, current(client, work)["canon_version"],
                    action="reject" if approved_status == "REJECTED" else "approve",
                    status="CANON" if approved_status == "REJECTED" else approved_status)
    assert result.status_code == 200, result.text
    protected = result.json()["entry"]
    entry_action(client, protected, current(client, work), "trash", expected=422)
    actual = client.get(P + f"/novels/{work['id']}/bible").json()["items"]
    assert next(r for r in actual if r["id"] == entry["id"])["status"] == approved_status


def test_inactive_canon_history_cannot_be_trashed_as_idea(studio):
    _, client, _ = studio
    work = novel(client)
    entry = idea(client, work["id"], details={"name": "Анна"})
    approved = decide(client, entry, current(client, work)["canon_version"]).json()["entry"]
    replacement = client.post(P + f"/bible/{entry['id']}/replacement", json={
        "base_version": approved["version"], "title": "Марина", "content": "Новое имя Марина.",
        "details": {"name": "Марина"}}).json()
    assert decide(client, replacement, current(client, work)["canon_version"]).status_code == 200
    rows = client.get(P + f"/novels/{work['id']}/bible").json()["items"]
    retired = next(r for r in rows if r["id"] == entry["id"])
    assert not retired["active"]
    entry_action(client, retired, current(client, work), "trash", expected=422)
    assert client.get(P + f"/novels/{work['id']}/rejections").json()["items"][0]["phrase"] == "Анна"


def test_trash_restore_compare_versions_and_cannot_replay_stale_actions(studio):
    _, client, _ = studio
    work = novel(client)
    changed = client.patch(P + f"/novels/{work['id']}", json={"base_version": work["version"], "title": "Новое имя"}).json()
    novel_action(client, work, "trash", expected=409)
    removed = novel_action(client, changed, "trash")
    novel_action(client, removed, "trash", expected=409)
    novel_action(client, changed, "restore", expected=409)
    restored = novel_action(client, removed, "restore")
    novel_action(client, restored, "restore", expected=409)
    entry = idea(client, work["id"])
    before = current(client, work)
    outcome = entry_action(client, entry, before, "trash")
    entry_action(client, entry, outcome["novel"], "restore", expected=409)
    entry_action(client, outcome["entry"], before, "restore", expected=409)
    assert client.get(P + f"/novels/{work['id']}/trash/ideas").json()["total"] == 1
    entry_action(client, outcome["entry"], outcome["novel"], "restore")


@pytest.mark.parametrize("action_target", ["novel", "idea"])
def test_context_change_cancels_jobs_and_releases_locks_without_erasing_candidates(studio, action_target):
    app, client, _ = studio
    work = novel(client)
    job_ids = []
    for status in ("queued", "running", "awaiting_author", "needs_author_review"):
        page = chapter(client, work["id"])
        job_ids.append(seed_job(app, current(client, work), page, status))
    entry = idea(client, work["id"])
    before = current(client, work)
    if action_target == "novel":
        deleted = novel_action(client, before, "trash")
        novel_action(client, deleted, "restore")
    else:
        deleted = entry_action(client, entry, before, "trash")
        entry_action(client, deleted["entry"], deleted["novel"], "restore")
    with app.state.session_factory() as db:
        jobs = [db.get(GenerationJob, job_id) for job_id in job_ids]
        assert all(j.status == "cancelled" and j.cancel_requested for j in jobs)
        assert all(j.active_chapter_id is None and j.lease_owner is None and j.lease_until is None for j in jobs)
        assert all(j.artifacts["draft_text"] == "Сохранённый вариант ИИ." for j in jobs)
        assert all(j.usage["cost_usd"] == 0.01 for j in jobs)


def test_other_author_cannot_list_delete_restore_or_read_trash(studio):
    app, client, _ = studio
    work = novel(client)
    entry = idea(client, work["id"])
    removed_entry = entry_action(client, entry, current(client, work), "trash")
    removed_novel = novel_action(client, removed_entry["novel"], "trash")
    with app.state.session_factory() as db:
        db.add(User(email="other@example.test", password_hash=hash_password(PASSWORD)))
        db.commit()
    with TestClient(app) as other:
        sign_in(other, "other@example.test")
        assert other.get(P + "/trash/novels").json()["total"] == 0
        assert other.get(P + f"/novels/{work['id']}/trash/ideas").status_code == 404
        for action in ("trash", "restore"):
            novel_action(other, removed_novel, action, expected=404)
            entry_action(other, removed_entry["entry"], removed_entry["novel"], action, expected=404)
    assert client.get(P + "/trash/novels").json()["total"] == 1


def test_trash_requires_csrf_and_idea_restore_requires_active_parent(studio):
    _, client, _ = studio
    work = novel(client)
    entry = idea(client, work["id"])
    headers = dict(client.headers)
    client.headers.pop("X-CSRF-Token")
    response = client.post(P + f"/novels/{work['id']}/trash", json={
        "base_version": work["version"], "expected_canon_version": work["canon_version"]})
    assert response.status_code == 403
    client.headers.update(headers)
    removed_entry = entry_action(client, entry, current(client, work), "trash")
    removed_novel = novel_action(client, removed_entry["novel"], "trash")
    entry_action(client, removed_entry["entry"], removed_novel, "restore", expected=404)
    restored_novel = novel_action(client, removed_novel, "restore")
    entry_action(client, removed_entry["entry"], restored_novel, "restore")


def test_trash_and_restore_survive_server_and_database_reopen(studio):
    _, client, settings = studio
    work = novel(client)
    page = chapter(client, work["id"])
    saved = save(client, page["id"], page["current_revision"]["id"], "Текст до удаления.").json()
    removed = novel_action(client, current(client, work), "trash")
    cookies = dict(client.cookies)
    reopened_app = create_app(settings)
    with TestClient(reopened_app) as reopened:
        reopened.cookies.update(cookies)
        session = reopened.get(P + "/session").json()
        reopened.headers.update({"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]})
        assert reopened.get(P + "/novels").json()["total"] == 0
        trashed = reopened.get(P + "/trash/novels").json()["items"][0]
        assert trashed["id"] == removed["id"]
        novel_action(reopened, trashed, "restore")
        assert reopened.get(P + f"/chapters/{page['id']}").json()["current_revision"]["id"] == saved["current_revision"]["id"]


def test_migration_from_0002_preserves_account_session_and_manuscript(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}",
                        allowed_origins=[ORIGIN], cookie_secure=False, openai_api_key="")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "0002_generation")
    engine = make_engine(settings.database_url)
    metadata = MetaData()
    metadata.reflect(engine)
    now = utcnow()
    user_id, novel_id, chapter_id, revision_id, entry_id = [str(uuid4()) for _ in range(5)]
    with engine.begin() as connection:
        connection.execute(insert(metadata.tables["users"]).values(
            id=user_id, email=EMAIL, password_hash=hash_password(PASSWORD), created_at=now))
        connection.execute(insert(metadata.tables["novels"]).values(
            id=novel_id, owner_id=user_id, title="Старый роман", genre="романтика", concept="Идея",
            version=4, canon_version=7, settings={}, created_at=now, updated_at=now))
        connection.execute(insert(metadata.tables["chapters"]).values(
            id=chapter_id, novel_id=novel_id, number=1, title="Существующая глава", status="DRAFT",
            current_revision_id=None, approved_revision_id=None, created_at=now, updated_at=now))
        connection.execute(insert(metadata.tables["revisions"]).values(
            id=revision_id, chapter_id=chapter_id, parent_revision_id=None, text="Рукопись до обновления.",
            source="manual", mutation_id=str(uuid4()), payload_hash="a" * 64, deleted_fragments=[], created_at=now))
        connection.execute(metadata.tables["chapters"].update().where(
            metadata.tables["chapters"].c.id == chapter_id).values(current_revision_id=revision_id))
        connection.execute(insert(metadata.tables["bible_entries"]).values(
            id=entry_id, novel_id=novel_id, kind="idea", title="Старая идея", content="Сохранить эту идею.",
            status="IDEA", version=3, active=True, supersedes_id=None, available_from_chapter=None,
            details={}, created_at=now))
    engine.dispose()
    legacy_app = create_app(settings)
    with TestClient(legacy_app) as legacy:
        sign_in(legacy)
        cookies = dict(legacy.cookies)
    assert backup(settings, tmp_path / "before-update.db").is_file()
    migrate(settings)
    migrate(settings)
    updated = create_app(settings)
    with TestClient(updated) as client:
        client.cookies.update(cookies)
        assert client.get(P + "/session").json()["user"]["email"] == EMAIL
        work = client.get(P + f"/novels/{novel_id}").json()
        assert work["version"] == 4 and work["canon_version"] == 7 and work["deleted_at"] is None
        assert client.get(P + f"/chapters/{chapter_id}").json()["current_revision"]["text"] == "Рукопись до обновления."
        row = client.get(P + f"/novels/{novel_id}/bible").json()["items"][0]
        assert row["id"] == entry_id and row["version"] == 3 and row["deleted_at"] is None
