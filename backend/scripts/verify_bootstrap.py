"""Проверка правил Git и реального запуска API. Запускать из любой папки."""

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def verify_gitignore() -> None:
    if not (ROOT / ".git").exists():
        print("Архив без истории Git: проверка gitignore пропущена.")
        return

    paths = [
        ".env",
        "backend/.env",
        ".venv/ignored.py",
        "backend/data/check.sqlite3",
        "node_modules/ignored.js",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        input=b"\0".join(path.encode("utf-8") for path in paths) + b"\0",
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    ignored = set(result.stdout.decode("utf-8").rstrip("\0").split("\0"))
    missing = set(paths) - ignored
    if missing:
        raise RuntimeError(f"Git не игнорирует нужные пути: {sorted(missing)}")

    example = subprocess.run(
        ["git", "check-ignore", "--no-index", ".env.example"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if example.returncode != 1:
        raise RuntimeError(".env.example должен оставаться доступен для Git.")
    print("Правила .gitignore проверены.")


def verify_server() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--app-dir", str(BACKEND),
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=ROOT,
    )
    try:
        deadline = time.monotonic() + 30
        address = f"http://127.0.0.1:{port}/api/health"
        while True:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Сервер завершился до проверки: код {process.returncode}"
                )
            try:
                with urlopen(address, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                break
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Сервер не ответил за 30 секунд.")
                time.sleep(0.2)

        expected = {
            "status": "ok",
            "service": "Novel Studio",
            "version": "0.2.0",
            "stage": "core",
        }
        if payload != expected:
            raise RuntimeError(f"Неожиданный ответ API: {payload!r}")
        if process.poll() is not None:
            raise RuntimeError("Процесс сервера завершился во время проверки.")
        print("Реальный HTTP-запрос /api/health выполнен успешно.")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    verify_gitignore()
    verify_server()
