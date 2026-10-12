"""Real export checks: immutable approval, valid DOCX, isolation, ordering."""
from contextlib import ExitStack
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile
import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.auth import hash_password
from app.config import Settings
from app.database import Base
from app.main import create_app
from app.models import User

def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()

@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setenv('NOVEL_OPENAI_API_KEY', '')
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'exports.db').as_posix()}", allowed_origins=['http://testserver'], cookie_secure=False)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    password = 'Novel Studio test password 2026!'
    with Session(app.state.engine) as db:
        for email in ('writer@example.test', 'other@example.test'):
            db.add(User(email=email, password_hash=hash_password(password)))
        db.commit()
    with ExitStack() as stack:
        author, other, anonymous = [stack.enter_context(TestClient(app)) for _ in range(3)]
        for client, email in ((author, 'writer@example.test'), (other, 'other@example.test')):
            client.headers['Origin'] = 'http://testserver'
            client.headers['X-CSRF-Token'] = ok(client.get('/api/v1/session'))['csrf_token']
            ok(client.post('/api/v1/auth/login', json={'email': email, 'password': password}))
            session = ok(client.get('/api/v1/session'))
            assert session['user']['email'] == email
            client.headers['X-CSRF-Token'] = session['csrf_token']
        yield author, other, anonymous

def novel_and_chapter(client):
    novel = ok(client.post('/api/v1/novels', json={'title': 'Наследство'}), 201)
    chapter = ok(client.post(f"/api/v1/novels/{novel['id']}/chapters", json={'title': 'Письмо', 'number': 1}), 201)
    return novel, ok(client.get(f"/api/v1/chapters/{chapter['id']}"))

def save(client, chapter, base, text):
    return ok(client.put(f"/api/v1/chapters/{chapter['id']}/text", json={'base_revision_id': base, 'text': text, 'mutation_id': str(uuid4())}))['current_revision']

def export(client, novel, file_format, selection):
    response = client.get(f"/api/v1/novels/{novel['id']}/export", params={'format': file_format, 'selection': selection})
    assert response.status_code == 200, response.text
    assert 'attachment' in response.headers['content-disposition'].lower()
    return response

def test_current_and_approved_exports_are_distinct(clients):
    author, _, _ = clients
    novel, chapter = novel_and_chapter(author)
    old = 'Под вечер Вера открыла конверт.\n\nПисьмо содержало неожиданное условие.'
    approved = save(author, chapter, chapter['current_revision']['id'], old)
    ok(author.post(f"/api/v1/chapters/{chapter['id']}/approve", json={'base_revision_id': approved['id']}))
    new = 'Утром Вера отложила письмо.\n\nЭто новая ручная редакция.'
    save(author, chapter, approved['id'], new)
    current = export(author, novel, 'txt', 'current').content.decode('utf-8-sig')
    published = export(author, novel, 'txt', 'approved').content.decode('utf-8-sig')
    assert novel['title'] in current and chapter['title'] in current
    assert new in current and old not in current
    assert old in published and new not in published

def test_docx_is_readable_and_preserves_paragraphs(clients):
    author, _, _ = clients
    novel, chapter = novel_and_chapter(author)
    text = 'Героиня задержалась у окна.\n\n— Я всё прочитала, — сказала она.\nСлова прозвучали спокойно.'
    save(author, chapter, chapter['current_revision']['id'], text)
    response = export(author, novel, 'docx', 'current')
    assert response.headers['content-type'].startswith('application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    with ZipFile(BytesIO(response.content)) as archive:
        assert 'word/document.xml' in archive.namelist()
        assert '[Content_Types].xml' in archive.namelist()
    paragraphs = '\n'.join(p.text for p in Document(BytesIO(response.content)).paragraphs)
    assert novel['title'] in paragraphs and chapter['title'] in paragraphs
    for fragment in text.split('\n'):
        if fragment:
            assert fragment in paragraphs

def test_export_enforces_author_ownership(clients):
    author, other, anonymous = clients
    novel, chapter = novel_and_chapter(author)
    private = 'Частная рукопись первого автора.'
    save(author, chapter, chapter['current_revision']['id'], private)
    for file_format in ('txt', 'docx'):
        path = f"/api/v1/novels/{novel['id']}/export"
        params = {'format': file_format, 'selection': 'current'}
        denied = other.get(path, params=params)
        assert denied.status_code == 404 and private not in denied.text
        assert anonymous.get(path, params=params).status_code == 401

def test_unapproved_text_is_excluded(clients):
    author, _, _ = clients
    novel, chapter = novel_and_chapter(author)
    text = 'Ещё не утверждённый черновик.'
    save(author, chapter, chapter['current_revision']['id'], text)
    published = export(author, novel, 'txt', 'approved').content.decode('utf-8-sig')
    assert novel['title'] in published
    assert text not in published and chapter['title'] not in published

def test_chapters_are_exported_in_number_order(clients):
    author, _, _ = clients
    novel = ok(author.post('/api/v1/novels', json={'title': 'Последовательность'}), 201)
    for number, title in ((10, 'Десятая сцена'), (2, 'Вторая сцена')):
        chapter = ok(author.post(f"/api/v1/novels/{novel['id']}/chapters", json={'title': title, 'number': number}), 201)
        chapter = ok(author.get(f"/api/v1/chapters/{chapter['id']}"))
        save(author, chapter, chapter['current_revision']['id'], f'Текст главы {number}.')
    text = export(author, novel, 'txt', 'current').content.decode('utf-8-sig')
    assert text.index('Вторая сцена') < text.index('Десятая сцена')

@pytest.mark.parametrize('params', [{'format': 'pdf', 'selection': 'current'}, {'format': 'txt', 'selection': 'all'}])
def test_unknown_export_options_are_rejected(clients, params):
    author, _, _ = clients
    novel, _ = novel_and_chapter(author)
    assert author.get(f"/api/v1/novels/{novel['id']}/export", params=params).status_code == 422
