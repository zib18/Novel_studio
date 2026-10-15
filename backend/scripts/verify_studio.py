"""Real Uvicorn/database smoke; no OpenAI calls. Helpers support browser tests."""
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4
import httpx
from docx import Document

BACKEND = Path(__file__).resolve().parents[1]

def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]

def make_env(database_path, origin):
    env = dict(os.environ)
    env.update(PYTHONPATH=str(BACKEND), PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    env['NOVEL_DATABASE_URL'] = env.get('NOVEL_TEST_DATABASE_URL', '').strip() or f'sqlite:///{Path(database_path).resolve().as_posix()}'
    env['NOVEL_ALLOWED_ORIGINS'] = json.dumps([origin])
    env['NOVEL_COOKIE_SECURE'] = 'false'
    env['NOVEL_OPENAI_API_KEY'] = ''
    env.pop('OPENAI_API_KEY', None)
    return env

def prepare_database(env, email, password):
    def run(args, child_env):
        result = subprocess.run(args, cwd=BACKEND, env=child_env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        if result.returncode:
            raise RuntimeError(result.stdout[-6000:] + result.stderr[-6000:])
    run([sys.executable, '-m', 'app.cli', 'migrate'], env)
    source = '''
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.auth import hash_password
from app.config import Settings
from app.models import User
engine = create_engine(Settings().database_url)
with Session(engine) as db:
    db.add(User(email=os.environ['NOVEL_VERIFY_EMAIL'], password_hash=hash_password(os.environ['NOVEL_VERIFY_PASSWORD'])))
    db.commit()
engine.dispose()
'''
    run([sys.executable, '-c', source], dict(env, NOVEL_VERIFY_EMAIL=email, NOVEL_VERIFY_PASSWORD=password))

class ManagedServer:
    def __init__(self, env, port, log_directory):
        self.env, self.port, self.log_directory = env, port, Path(log_directory)
        self.base_url = f'http://127.0.0.1:{port}'
        self.process, self.log_handle, self.log_paths = None, None, []
    def start(self):
        path = self.log_directory / f'server-{len(self.log_paths)}.log'
        self.log_paths.append(path)
        self.log_handle = path.open('wb')
        self.process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(self.port)], cwd=BACKEND, env=self.env, stdout=self.log_handle, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 40
        with httpx.Client(base_url=self.base_url, timeout=1, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('Uvicorn exited before readiness.')
                try:
                    if client.get('/api/health').status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
        raise RuntimeError('Uvicorn readiness timeout.')
    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None
    def restart(self):
        self.stop()
        self.start()
    def logs(self):
        if self.log_handle is not None:
            self.log_handle.flush()
        return '\n'.join(path.read_text(encoding='utf-8', errors='replace')[-6000:] for path in self.log_paths)

@contextmanager
def running_server(env, port):
    with TemporaryDirectory(prefix='novel-server-') as directory:
        server = ManagedServer(env, port, directory)
        try:
            server.start()
            yield server
        except BaseException:
            print(server.logs(), file=sys.stderr)
            raise
        finally:
            server.stop()

def request_json(client, method, path, *, expected=200, payload=None):
    response = client.request(method, path, json=payload)
    assert response.status_code == expected, f'{method} {path}: {response.status_code} {response.text}'
    return response.json()

def login(client, email, password):
    session = request_json(client, 'GET', '/api/v1/session')
    client.headers['X-CSRF-Token'] = session['csrf_token']
    request_json(client, 'POST', '/api/v1/auth/login', payload={'email': email, 'password': password})
    session = request_json(client, 'GET', '/api/v1/session')
    assert session['user']['email'] == email
    client.headers['X-CSRF-Token'] = session['csrf_token']

def verify_trash(desktop, mobile, server, novel, chapter, revision):
    novel_path = f"/api/v1/novels/{novel['id']}"
    chapter_path = f"/api/v1/chapters/{chapter['id']}"
    untouched = request_json(desktop, 'POST', '/api/v1/novels', expected=201,
                             payload={'title': 'Другой независимый роман'})
    entry = request_json(desktop, 'POST', novel_path + '/bible', expected=201, payload={
        'kind': 'idea', 'title': 'Проверяемая идея', 'content': 'Вариант на рассмотрении.', 'status': 'IDEA'})
    work = request_json(desktop, 'GET', novel_path)
    before = {'base_version': entry['version'], 'expected_canon_version': work['canon_version']}
    result = request_json(desktop, 'POST', f"/api/v1/bible/{entry['id']}/trash", payload=before)
    assert result['entry']['deleted_at']
    assert entry['id'] not in {e['id'] for e in request_json(mobile, 'GET', novel_path + '/bible')['items']}
    assert request_json(mobile, 'GET', novel_path + '/trash/ideas')['items'][0]['id'] == entry['id']
    request_json(mobile, 'POST', f"/api/v1/bible/{entry['id']}/restore", expected=409, payload=before)
    restored = request_json(desktop, 'POST', f"/api/v1/bible/{entry['id']}/restore", payload={
        'base_version': result['entry']['version'], 'expected_canon_version': result['novel']['canon_version']})
    assert restored['entry']['status'] == 'IDEA' and restored['entry']['deleted_at'] is None
    work = request_json(desktop, 'GET', novel_path)
    old_action = {'base_version': work['version'], 'expected_canon_version': work['canon_version']}
    trashed = request_json(desktop, 'POST', novel_path + '/trash', payload=old_action)
    active_ids = {n['id'] for n in request_json(mobile, 'GET', '/api/v1/novels')['items']}
    assert novel['id'] not in active_ids and untouched['id'] in active_ids
    request_json(mobile, 'GET', chapter_path, expected=404)
    request_json(mobile, 'PUT', chapter_path + '/text', expected=404, payload={
        'base_revision_id': revision['id'], 'text': 'Нельзя перезаписать удалённый роман.', 'mutation_id': str(uuid4())})
    server.restart()
    trash = request_json(mobile, 'GET', '/api/v1/trash/novels')['items']
    assert next(row for row in trash if row['id'] == novel['id'])['deleted_at']
    request_json(mobile, 'GET', novel_path, expected=404)
    returned = request_json(desktop, 'POST', novel_path + '/restore', payload={
        'base_version': trashed['version'], 'expected_canon_version': trashed['canon_version']})
    assert returned['id'] == novel['id'] and returned['deleted_at'] is None
    request_json(mobile, 'POST', novel_path + '/trash', expected=409, payload=old_action)
    saved_again = request_json(mobile, 'GET', chapter_path)['current_revision']
    assert saved_again['id'] == revision['id'] and saved_again['text'] == revision['text']
    assert request_json(mobile, 'GET', chapter_path + '/revisions')['total'] >= 3
    assert request_json(desktop, 'GET', novel_path + '/trash/ideas')['total'] == 0
    assert request_json(mobile, 'GET', f"/api/v1/novels/{untouched['id']}")['deleted_at'] is None


def verify():
    with TemporaryDirectory(prefix='novel-smoke-') as directory:
        database_path = Path(directory) / 'novel.db'
        port = free_port()
        origin = f'http://127.0.0.1:{port}'
        env = make_env(database_path, origin)
        email, password = 'smoke@example.test', 'Novel Studio smoke password 2026!'
        prepare_database(env, email, password)
        with running_server(env, port) as server:
            options = dict(base_url=origin, headers={'Origin': origin}, timeout=10, trust_env=False)
            with httpx.Client(**options) as desktop, httpx.Client(**options) as mobile:
                login(desktop, email, password)
                login(mobile, email, password)
                novel = request_json(desktop, 'POST', '/api/v1/novels', expected=201, payload={'title': 'Проверка студии'})
                chapter = request_json(desktop, 'POST', f"/api/v1/novels/{novel['id']}/chapters", expected=201, payload={'title': 'Письмо', 'number': 1})
                path = f"/api/v1/chapters/{chapter['id']}"
                initial = request_json(mobile, 'GET', path)['current_revision']['id']
                approved_text = 'Под вечер Вера вернулась к письму.\n\nВ нём оказалось условие наследства.'
                save = {'base_revision_id': initial, 'text': approved_text, 'mutation_id': str(uuid4())}
                saved = request_json(desktop, 'PUT', path + '/text', payload=save)['current_revision']
                replay = request_json(desktop, 'PUT', path + '/text', payload=save)['current_revision']
                assert replay['id'] == saved['id']
                assert request_json(mobile, 'GET', path)['current_revision']['text'] == approved_text
                conflict = mobile.put(path + '/text', json={'base_revision_id': initial, 'text': 'Устаревшая правка.', 'mutation_id': str(uuid4())})
                assert conflict.status_code == 409, conflict.text
                assert request_json(desktop, 'GET', path)['current_revision']['id'] == saved['id']
                request_json(desktop, 'POST', path + '/approve', payload={'base_revision_id': saved['id']})
                current_text = 'На следующее утро Вера перечитала письмо.\n\nТеперь она знала, что спросит у нотариуса.'
                current = request_json(desktop, 'PUT', path + '/text', payload={'base_revision_id': saved['id'], 'text': current_text, 'mutation_id': str(uuid4())})['current_revision']
                export_path = f"/api/v1/novels/{novel['id']}/export"
                def exported(file_format, selection):
                    response = desktop.get(export_path, params={'format': file_format, 'selection': selection})
                    assert response.status_code == 200, response.text
                    return response.content
                assert current_text in exported('txt', 'current').decode('utf-8-sig')
                published = exported('txt', 'approved').decode('utf-8-sig')
                assert approved_text in published and current_text not in published
                docx_text = '\n'.join(p.text for p in Document(BytesIO(exported('docx', 'current'))).paragraphs)
                assert 'На следующее утро Вера' in docx_text and 'Теперь она знала' in docx_text
                failed = desktop.post(f"/api/v1/novels/{novel['id']}/jobs", headers={'Idempotency-Key': str(uuid4())}, json={'chapter_id': chapter['id'], 'base_revision_id': current['id'], 'kind': 'write', 'instructions': 'Продолжить главу.'})
                assert failed.status_code == 503, failed.text
                assert request_json(desktop, 'GET', path)['current_revision']['text'] == current_text
                server.restart()
                durable = request_json(mobile, 'GET', path)['current_revision']
                assert durable['id'] == current['id'] and durable['text'] == current_text
                history = request_json(mobile, 'GET', path + '/revisions')
                assert history['total'] >= 3
                assert {saved['id'], current['id']} <= {r['id'] for r in history['items']}
                assert approved_text in exported('txt', 'approved').decode('utf-8-sig')
                verify_trash(desktop, mobile, server, novel, chapter, current)
        if env['NOVEL_DATABASE_URL'].startswith('sqlite:'):
            assert database_path.is_file()
    print('Novel Studio HTTP smoke passed: sessions, saves, conflicts, approval, exports, missing-key error, persistence.')

if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    verify()
