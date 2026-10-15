"""Exercise the actual Windows updater, npm.cmd, migrations and installed HTTP server."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import httpx

ROOT = Path(__file__).resolve().parents[2]
EMAIL = "windows-update@example.test"
PASSWORD = "Windows-update-smoke-password"
TEXT = "Последняя ручная правка автора пережила обновление Windows.\nИстория романа сохранена."


def run(args, cwd, env, timeout=240):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}):\n{result.stdout[-8000:]}\n{result.stderr[-8000:]}")
    return result.stdout


def copy_release(destination):
    for name in ("backend", "frontend", "tools", "docs"):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, destination / name,
                            ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "dist",
                                                         "data", ".env", "__pycache__", ".pytest_cache", "*.pyc"))
    for name in ("README.md", ".gitignore", "Update-Windows.cmd"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, destination / name)


def verify_database(path, ids):
    assert path.is_file(), path
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT email FROM users WHERE id=?", (ids["user"],)).fetchone()[0] == EMAIL
        assert db.execute("SELECT title FROM novels WHERE id=?", (ids["novel"],)).fetchone()[0] == "Роман до обновления"
        assert db.execute("SELECT current_revision_id FROM chapters WHERE id=?", (ids["chapter"],)).fetchone()[0] == ids["revision"]
        assert db.execute("SELECT text FROM revisions WHERE id=?", (ids["revision"],)).fetchone()[0] == TEXT


def main():
    if os.name != "nt":
        raise RuntimeError("This smoke must run on Windows.")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    clean = {k: v for k, v in os.environ.items()
             if not k.upper().startswith("NOVEL_") and k.upper() not in ("OPENAI_API_KEY", "PYTHONPATH")}
    clean.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", NOVEL_OPENAI_API_KEY="")
    with tempfile.TemporaryDirectory(prefix="novel-windows-update-") as directory:
        temporary = Path(directory)
        source, target = temporary / "release", temporary / "installed"
        source.mkdir()
        target.mkdir()
        copy_release(source)
        copy_release(target)
        private = ("# Private author configuration: preserve byte for byte.\r\n"
                   "NOVEL_OPENAI_API_KEY=windows-canary-not-a-real-key\r\n"
                   f"NOVEL_ALLOWED_ORIGINS={json.dumps([origin])}\r\n").encode("utf-8")
        (target / "backend/.env").write_bytes(private)
        (source / "backend/.env").write_text("NOVEL_OPENAI_API_KEY=must-not-overwrite-author\n", encoding="utf-8")
        (target / "frontend/dist").mkdir()
        (target / "frontend/dist/index.html").write_text("old-interface", encoding="utf-8")
        (target / "README.md").write_text("Old installed documentation", encoding="utf-8")
        run([sys.executable, "-m", "venv", "--system-site-packages", target / ".venv"], target, clean)
        python = target / ".venv/Scripts/python.exe"
        interpreter_hash = hashlib.sha256(python.read_bytes()).hexdigest()
        child = dict(clean, PYTHONPATH=str(target / "backend"))
        run([python, "-m", "app.cli", "migrate"], target / "backend", child)
        seed = r"""
import json
from uuid import uuid4
from app.auth import hash_password
from app.config import Settings
from app.database import make_engine, build_session_factory
from app.models import User, Novel, Chapter, Revision
from app.services import payload_hash
engine = make_engine(Settings().database_url)
with build_session_factory(engine)() as db:
    user = User(email="windows-update@example.test", password_hash=hash_password("Windows-update-smoke-password"))
    db.add(user)
    db.flush()
    novel = Novel(owner_id=user.id, title="Роман до обновления")
    db.add(novel)
    db.flush()
    chapter = Chapter(novel_id=novel.id, number=1, title="Сохранённая глава")
    db.add(chapter)
    db.flush()
    text = "Последняя ручная правка автора пережила обновление Windows.\nИстория романа сохранена."
    revision = Revision(chapter_id=chapter.id, text=text, source="manual",
                        mutation_id=str(uuid4()), payload_hash=payload_hash(None, text))
    db.add(revision)
    db.flush()
    chapter.current_revision_id = revision.id
    db.commit()
    print(json.dumps(dict(user=user.id, novel=novel.id, chapter=chapter.id, revision=revision.id)))
engine.dispose()
"""
        ids = json.loads(run([python, "-c", seed], target / "backend", child).strip())
        database = target / "backend/data/novel.db"
        verify_database(database, ids)
        run([sys.executable, source / "tools/update_windows.py", "--target", target], source, clean)
        assert (target / "backend/.env").read_bytes() == private
        assert hashlib.sha256(python.read_bytes()).hexdigest() == interpreter_hash
        assert (target / "README.md").read_bytes() == (source / "README.md").read_bytes()
        assert (target / "frontend/dist/index.html").is_file()
        assert "old-interface" not in (target / "frontend/dist/index.html").read_text(encoding="utf-8")
        backups = list((target / "backend/data/backups").glob("before-update-*.db"))
        assert len(backups) == 1
        verify_database(database, ids)
        verify_database(backups[0], ids)
        with (temporary / "installed-server.log").open("wb") as log:
            server = subprocess.Popen([str(python), "-m", "uvicorn", "app.main:app",
                                       "--host", "127.0.0.1", "--port", str(port)],
                                      cwd=target / "backend", env=child, stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=origin, timeout=2, trust_env=False) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        if server.poll() is not None:
                            raise RuntimeError("Updated server exited before readiness.")
                        try:
                            if client.get("/api/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Updated HTTP server readiness timed out.")
                        time.sleep(0.15)
                    index = client.get("/")
                    assert index.status_code == 200 and 'id="root"' in index.text
                    session = client.get("/api/v1/session").json()
                    client.headers.update({"Origin": origin, "X-CSRF-Token": session["csrf_token"]})
                    login = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
                    assert login.status_code == 200, login.text
                    assert login.json()["user"]["email"] == EMAIL
                    novels = client.get("/api/v1/novels").json()["items"]
                    assert [n["id"] for n in novels] == [ids["novel"]]
                    chapter = client.get(f"/api/v1/chapters/{ids['chapter']}").json()
                    assert chapter["current_revision"]["id"] == ids["revision"]
                    assert chapter["current_revision"]["text"] == TEXT
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
        print("Actual Windows update passed: npm.cmd build, pip, backup, migrations, credentials, manuscript and installed HTTP server.")


if __name__ == "__main__":
    main()
