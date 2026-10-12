"""Core tests use real SQLite, migrations, authenticated HTTP and durable records."""
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.auth import hash_password, verify_password
from app.cli import backup, create_user, migrate
from app.config import Settings
from app.main import create_app
from app.models import User

PASSWORD = "long-writer-password"
EMAIL = "author@example.test"
ORIGIN = "http://testserver"
P = "/api/v1"


def sign_in(client, email=EMAIL):
    anonymous = client.get(P + "/session")
    assert anonymous.status_code == 200
    result = client.post(P + "/auth/login", json={"email": email, "password": PASSWORD},
                         headers={"Origin": ORIGIN, "X-CSRF-Token": anonymous.json()["csrf_token"]})
    assert result.status_code == 200, result.text
    client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]})
    return result.json()


@pytest.fixture
def studio(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'novel.db').as_posix()}",
                        allowed_origins=[ORIGIN], cookie_secure=False, openai_api_key="")
    migrate(settings)
    migrate(settings)
    app = create_app(settings)
    with app.state.session_factory() as db:
        create_user(db, EMAIL, PASSWORD)
    with TestClient(app) as client:
        sign_in(client)
        yield app, client, settings


def novel(client, title="Наследство"):
    response = client.post(P + "/novels", json={"title": title, "genre": "романтика"})
    assert response.status_code == 201, response.text
    return response.json()


def chapter(client, novel_id):
    response = client.post(P + f"/novels/{novel_id}/chapters", json={"title": "Предложение"})
    assert response.status_code == 201, response.text
    return response.json()


def save(client, chapter_id, base, text, mutation=None):
    return client.put(P + f"/chapters/{chapter_id}/text",
                      json={"base_revision_id": base, "text": text, "mutation_id": mutation or str(uuid4())})


def idea(client, novel_id, **values):
    payload = {"kind": "character", "title": "Анна", "content": "Героиня получает наследство.", **values}
    response = client.post(P + f"/novels/{novel_id}/bible", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def decide(client, entry, canon_version, action="approve", status="CANON"):
    return client.post(P + f"/bible/{entry['id']}/decision",
                       json={"action": action, "base_version": entry["version"],
                             "expected_canon_version": canon_version, "target_status": status,
                             "reason": "Прямое решение автора"})


def test_password_hash_uses_random_salt():
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)
    assert first != second and PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert not verify_password("incorrect", first)


def test_session_csrf_rotation_and_origin_checks(studio):
    app, _, _ = studio
    with TestClient(app) as anonymous:
        assert anonymous.get(P + "/novels").status_code == 401
        response = anonymous.get(P + "/session")
        token = response.json()["csrf_token"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=lax" in response.headers["set-cookie"]
        assert anonymous.get(P + "/session").json()["csrf_token"] == token
        body = {"email": EMAIL, "password": PASSWORD}
        assert anonymous.post(P + "/auth/login", json=body).status_code == 403
        assert anonymous.post(P + "/auth/login", json=body,
                              headers={"Origin": "https://evil.example", "X-CSRF-Token": token}).status_code == 403
        login = anonymous.post(P + "/auth/login", json=body,
                               headers={"Origin": ORIGIN, "X-CSRF-Token": token})
        assert login.status_code == 200
        assert login.json()["csrf_token"] != token
        assert anonymous.post(P + "/novels", json={"title": "Роман"},
                              headers={"Origin": ORIGIN, "X-CSRF-Token": token}).status_code == 403


def test_two_devices_revisions_conflict_and_mutation_idempotence(studio):
    _, client, _ = studio
    work = novel(client)
    page = chapter(client, work["id"])
    base = page["current_revision"]["id"]
    mutation = str(uuid4())
    first = save(client, page["id"], base, "Последняя ручная правка автора.", mutation)
    assert first.status_code == 200
    current = first.json()["current_revision"]["id"]
    retry = save(client, page["id"], base, "Последняя ручная правка автора.", mutation)
    assert retry.status_code == 200
    assert retry.json()["saved_revision_id"] == current
    assert save(client, page["id"], base, "Другой текст.", mutation).status_code == 409
    conflict = save(client, page["id"], base, "Правка с устаревшего телефона.")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision_id"] == current
    final = client.get(P + f"/chapters/{page['id']}").json()
    assert final["current_revision"]["text"] == "Последняя ручная правка автора."
    revisions = client.get(P + f"/chapters/{page['id']}/revisions").json()
    assert revisions["total"] == 2


def test_idempotent_replay_ack_does_not_claim_new_remote_revision(studio):
    _, client, _ = studio
    page = chapter(client, novel(client)["id"])
    base = page["current_revision"]["id"]
    mutation = str(uuid4())
    first = save(client, page["id"], base, "Текст первого устройства.", mutation).json()
    first_id = first["current_revision"]["id"]
    second = save(client, page["id"], first_id, "Последний текст второго устройства.").json()
    replay = save(client, page["id"], base, "Текст первого устройства.", mutation).json()
    assert replay["saved_revision_id"] == first_id
    assert replay["saved_revision"]["id"] == first_id
    assert replay["saved_revision"]["text"] == "Текст первого устройства."
    assert replay["current_revision"]["id"] == second["current_revision"]["id"]


def test_manual_deletion_record_restore_creates_immutable_revision(studio):
    _, client, _ = studio
    page = chapter(client, novel(client)["id"])
    text = "Анна закрыла письмо. Эта удалённая автором фраза не должна возвращаться.\nОна вышла в сад."
    first = save(client, page["id"], page["current_revision"]["id"], text).json()["current_revision"]
    edited = "Анна закрыла письмо.\nОна вышла в сад."
    second = save(client, page["id"], first["id"], edited).json()["current_revision"]
    assert any("удалённая автором фраза" in fragment for fragment in second["deleted_fragments"])
    restored = client.post(P + f"/chapters/{page['id']}/restore",
                           json={"base_revision_id": second["id"], "revision_id": first["id"],
                                 "mutation_id": str(uuid4())})
    assert restored.status_code == 200
    latest = restored.json()["current_revision"]
    assert latest["id"] != first["id"] and latest["text"] == text and latest["source"] == "restore"
    assert client.get(P + f"/revisions/{first['id']}").json()["text"] == text
    assert client.get(P + f"/revisions/{second['id']}").json()["text"] == edited


def test_approval_locks_to_exact_revision(studio):
    _, client, _ = studio
    page = chapter(client, novel(client)["id"])
    base = page["current_revision"]["id"]
    assert client.post(P + f"/chapters/{page['id']}/approve",
                       json={"base_revision_id": base}).status_code == 422
    saved = save(client, page["id"], base, "Утверждаемый художественный текст.").json()
    latest = saved["current_revision"]["id"]
    assert client.post(P + f"/chapters/{page['id']}/approve",
                       json={"base_revision_id": base}).status_code == 422
    approved = client.post(P + f"/chapters/{page['id']}/approve",
                           json={"base_revision_id": latest})
    assert approved.status_code == 200 and approved.json()["approved_revision_id"] == latest
    assert approved.json()["status"] == "APPROVED"
    changed = save(client, page["id"], latest, "Следующая авторская правка.").json()
    assert changed["status"] == "DRAFT" and changed["approved_revision_id"] == latest


def test_proposals_rejection_and_canon_conflict(studio):
    _, client, _ = studio
    work = novel(client)
    first = idea(client, work["id"])
    second = idea(client, work["id"], title="Борис")
    assert first["status"] == "IDEA"
    rejected = decide(client, first, 1, action="reject")
    assert rejected.status_code == 200 and rejected.json()["entry"]["status"] == "REJECTED"
    assert decide(client, rejected.json()["entry"], 2).status_code == 422
    stale = decide(client, second, 1)
    assert stale.status_code == 409
    rows = client.get(P + f"/novels/{work['id']}/bible").json()["items"]
    assert next(row for row in rows if row["id"] == second["id"])["status"] == "IDEA"
    assert decide(client, second, 2).status_code == 200
    assert client.post(P + f"/novels/{work['id']}/bible",
                       json={"kind": "core", "title": "Самовольный канон",
                             "content": "", "status": "CANON"}).status_code == 422


def test_confirmed_character_rename_retires_old_fact_and_forbids_name(studio):
    _, client, _ = studio
    work = novel(client)
    first = idea(client, work["id"], details={"name": "Анна"})
    approved = decide(client, first, 1).json()["entry"]
    replacement = client.post(P + f"/bible/{first['id']}/replacement",
                              json={"base_version": approved["version"], "title": "Марина",
                                    "content": "Главную героиню зовут Марина.", "details": {"name": "Марина"}})
    assert replacement.status_code == 201 and replacement.json()["status"] == "PROPOSAL"
    assert client.get(P + f"/novels/{work['id']}/rejections").json()["total"] == 0
    result = decide(client, replacement.json(), 2)
    assert result.status_code == 200
    rows = client.get(P + f"/novels/{work['id']}/bible").json()["items"]
    assert not next(row for row in rows if row["id"] == first["id"])["active"]
    rules = client.get(P + f"/novels/{work['id']}/rejections").json()["items"]
    assert rules[0]["phrase"] == "Анна" and rules[0]["source_decision_id"] == result.json()["decision_id"]


def test_future_vault_requires_explicit_reveal_chapter(studio):
    _, client, _ = studio
    work = novel(client)
    no_chapter = idea(client, work["id"], kind="future_vault", title="Наследник", content="Борис — наследник")
    assert decide(client, no_chapter, 1, status="FUTURE_CANON").status_code == 422
    reveal = idea(client, work["id"], kind="future_vault", title="Тайна", available_from_chapter=15)
    result = decide(client, reveal, 1)
    assert result.status_code == 200
    assert result.json()["entry"]["status"] == "FUTURE_CANON"


def test_novels_and_authors_are_isolated(studio):
    app, client, _ = studio
    first = novel(client)
    second = novel(client, "Другой роман")
    page = chapter(client, first["id"])
    fact = idea(client, first["id"])
    assert client.get(P + f"/novels/{second['id']}/bible").json()["total"] == 0
    assert client.post(P + f"/novels/{second['id']}/rejections",
                       json={"phrase": "запрет", "description": "запрет", "scope": page["id"]}).status_code == 422
    with app.state.session_factory() as db:
        db.add(User(email="other@example.test", password_hash=hash_password(PASSWORD)))
        db.commit()
    with TestClient(app) as other:
        sign_in(other, "other@example.test")
        assert other.get(P + f"/novels/{first['id']}").status_code == 404
        assert other.get(P + f"/chapters/{page['id']}").status_code == 404
        assert other.get(P + f"/revisions/{page['current_revision']['id']}").status_code == 404
        assert decide(other, fact, 1).status_code == 404
        assert other.get(P + "/novels").json()["total"] == 0


def test_settings_validation_and_compare_and_swap(studio):
    _, client, _ = studio
    work = novel(client)
    changed = client.patch(P + f"/novels/{work['id']}", json={"base_version": 1, "title": "Новое имя"})
    assert changed.status_code == 200 and changed.json()["version"] == 2
    assert client.patch(P + f"/novels/{work['id']}",
                        json={"base_version": 1, "title": "Старая правка"}).status_code == 409
    assert client.patch(P + f"/novels/{work['id']}",
                        json={"base_version": 2, "settings": {"target_min_chars": 9000, "target_max_chars": 6000}}).status_code == 422
    assert client.patch(P + f"/novels/{work['id']}",
                        json={"base_version": 2, "settings": {"job_budget_usd": -1}}).status_code == 422


def test_database_session_and_chapters_survive_engine_reopen_and_backup(studio, tmp_path):
    app, client, settings = studio
    work = novel(client)
    page = chapter(client, work["id"])
    text = "Сохранённый текст остаётся после перезапуска сервера."
    save(client, page["id"], page["current_revision"]["id"], text)
    copied = backup(settings, tmp_path / "backup.db")
    assert copied.is_file()
    cookies = dict(client.cookies)
    second_app = create_app(settings)
    with TestClient(second_app) as reopened:
        reopened.cookies.update(cookies)
        assert reopened.get(P + "/session").json()["user"]["email"] == EMAIL
        assert reopened.get(P + f"/chapters/{page['id']}").json()["current_revision"]["text"] == text


def test_partial_settings_preserve_full_configuration_and_invalidate_context(studio):
    _, client, _ = studio
    created = client.post(P + "/novels", json={"title": "Настройки", "settings": {
        "target_min_chars": 2000, "target_max_chars": 4000,
        "job_budget_usd": 1.7, "max_review_passes": 5,
    }})
    assert created.status_code == 201
    work = created.json()
    changed = client.patch(P + f"/novels/{work['id']}", json={
        "base_version": 1, "settings": {"target_max_chars": 5000},
    })
    assert changed.status_code == 200, changed.text
    values = changed.json()["settings"]
    assert values["target_min_chars"] == 2000 and values["target_max_chars"] == 5000
    assert values["job_budget_usd"] == 1.7 and values["max_review_passes"] == 5
    assert changed.json()["canon_version"] == work["canon_version"] + 1
    invalid = client.patch(P + f"/novels/{work['id']}", json={
        "base_version": 2, "settings": {"target_min_chars": 6000},
    })
    assert invalid.status_code == 422
    explicit_null = client.patch(P + f"/novels/{work['id']}", json={
        "base_version": 2, "settings": {"job_budget_usd": None},
    })
    assert explicit_null.status_code == 422
    concept = client.patch(P + f"/novels/{work['id']}", json={
        "base_version": 2, "concept": "Последнее прямое решение автора.",
    })
    assert concept.status_code == 200 and concept.json()["canon_version"] == work["canon_version"] + 2


def test_large_repetitive_deletions_use_bounded_diff_and_capture_short_reply(monkeypatch):
    from app import services
    before = ("Я не уйду. " * 50000)[:500000]
    assert len(before) == 500000
    def forbidden_unbounded_matcher(*args, **kwargs):
        raise AssertionError("Large middle must not enter SequenceMatcher")
    with monkeypatch.context() as limited:
        limited.setattr(services.difflib, "SequenceMatcher", forbidden_unbounded_matcher)
        assert services.deleted_fragments(before, "") == [before]
        assert services.deleted_fragments(before, "Совсем новый текст.") == [before]
        assert services.deleted_fragments(before, before) == []
        removed = before[:490000]
        prefix, suffix = "Сцена началась.\n", "\nСцена закончилась."
        assert services.deleted_fragments(prefix + removed + suffix, prefix + suffix) == [removed]
    assert services.deleted_fragments("Я не уйду", "") == ["Я не уйду"]
    assert services.deleted_fragments("домохозяйка", "") == []
