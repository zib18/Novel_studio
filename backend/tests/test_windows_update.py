"""Real SQLite copies exercise updater preservation; npm/process calls are isolated."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import pytest

spec = importlib.util.spec_from_file_location(
    "novel_windows_updater", Path(__file__).resolve().parents[2] / "tools/update_windows.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


@pytest.fixture
def releases(tmp_path, monkeypatch):
    source, target = tmp_path / "release", tmp_path / "installed"
    for root, text in ((source, "new"), (target, "old")):
        for path, content in {"backend/app/cli.py": text, "backend/requirements.txt": "",
                              "frontend/package.json": "{}", "README.md": text}.items():
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding="utf-8")
    python = target / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"existing interpreter")
    (target / "backend/.env").write_text("private config", encoding="utf-8")
    database = target / "backend/data/novel.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE chapters (text TEXT)")
        db.execute("INSERT INTO chapters VALUES (?)", ("Ручная правка автора",))
    monkeypatch.setattr(updater, "ensure_stopped", lambda python: None)
    monkeypatch.setattr(updater.shutil, "which", lambda name: name)
    calls = []

    def runner(args, cwd):
        args = [str(a) for a in args]
        calls.append(args)
        if "backup" in args:
            path = Path(args[args.index("--output") + 1])
            path.parent.mkdir(parents=True)
            with sqlite3.connect(database) as original, sqlite3.connect(path) as backup:
                original.backup(backup)
        if "build" in args:
            dist = source / "frontend/dist"
            dist.mkdir()
            (dist / "index.html").write_text("new UI", encoding="utf-8")
    return source, target, python, database, calls, runner


def test_update_preserves_manuscripts_credentials_and_interpreter(releases):
    source, target, python, database, calls, runner = releases
    (source / "backend/.env").write_text("must not overwrite key", encoding="utf-8")
    (source / "backend/data").mkdir()
    (source / "backend/data/novel.db").write_bytes(b"must not overwrite manuscript")
    (source / ".venv").mkdir()
    (source / ".venv/extra").write_text("must not copy", encoding="utf-8")
    backup = updater.update(source, target, runner)
    assert "backup" in calls[0] and "migrate" in calls[-1]
    assert (target / "backend/.env").read_text(encoding="utf-8") == "private config"
    assert python.read_bytes() == b"existing interpreter"
    assert not (target / ".venv/extra").exists()
    assert (target / "backend/app/cli.py").read_text(encoding="utf-8") == "new"
    assert (target / "frontend/dist/index.html").read_text(encoding="utf-8") == "new UI"
    for path in (database, backup):
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT text FROM chapters").fetchone()[0] == "Ручная правка автора"


def test_failed_build_leaves_installed_sources_unchanged(releases):
    source, target, _, database, _, runner = releases

    def fail_build(args, cwd):
        if "build" in args:
            raise subprocess.CalledProcessError(1, args)
        runner(args, cwd)

    with pytest.raises(subprocess.CalledProcessError):
        updater.update(source, target, fail_build)
    assert (target / "backend/app/cli.py").read_text(encoding="utf-8") == "old"
    assert (target / "backend/.env").read_text(encoding="utf-8") == "private config"
    assert len(list((target / "backend/data/backups").glob("*.db"))) == 1


def test_updater_stops_before_copy_without_backup(releases):
    source, target, _, _, calls, _ = releases
    with pytest.raises(ValueError, match="Резервная копия не создана"):
        updater.update(source, target, lambda args, cwd: calls.append(args))
    assert len(calls) == 1
    assert (target / "backend/app/cli.py").read_text(encoding="utf-8") == "old"


def test_updater_requires_separate_directories(releases):
    source, target, *_ = releases
    for unsafe in (source, source / "nested"):
        with pytest.raises(ValueError, match="отдельную папку"):
            updater.check_paths(source, unsafe)
    assert updater.check_paths(source, target)[1] == target.resolve()


@pytest.fixture(scope="module")
def real_windows_venv(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("Windows process API")
    import venv
    root = tmp_path_factory.mktemp("real-updater-venv")
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(root)
    return root / "Scripts/python.exe"


def test_windows_process_guard_detects_running_interpreter(real_windows_venv, tmp_path):
    import time
    python = real_windows_venv
    updater.ensure_stopped(python)
    ready = tmp_path / "ready"
    code = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ready'); sys.stdin.read()"
    child = subprocess.Popen([str(python), "-c", code, str(ready)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "Real venv interpreter did not start"
        with pytest.raises(ValueError, match="Остановите сервер"):
            updater.ensure_stopped(python)
    finally:
        try:
            child.communicate(input="", timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
    assert child.returncode == 0
    updater.ensure_stopped(python)


def test_windows_process_guard_excludes_own_venv_launcher(real_windows_venv):
    probe = "import runpy,sys; runpy.run_path(sys.argv[1])['ensure_stopped'](sys.argv[2])"
    completed = subprocess.run(
        [str(real_windows_venv), "-c", probe, str(Path(updater.__file__).resolve()), str(real_windows_venv)],
        capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("error", [OSError("process query denied"), ValueError("cannot inspect process")])
def test_process_inspection_failure_stops_before_backup_or_copy(releases, monkeypatch, error):
    source, target, _, _, calls, runner = releases
    def fail(python):
        raise error
    monkeypatch.setattr(updater, "ensure_stopped", fail)
    with pytest.raises(type(error), match=str(error)):
        updater.update(source, target, runner)
    assert calls == []
    assert (target / "backend/app/cli.py").read_text(encoding="utf-8") == "old"
