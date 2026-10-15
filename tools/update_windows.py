"""Update an existing Windows installation from an extracted ZIP, preserving private data."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "data", "backups",
             "__pycache__", ".pytest_cache", ".vite", ".gradle", "build"}
SOURCE_DIRS = ("backend", "frontend", "docs", "tools")
SOURCE_FILES = ("README.md", ".gitignore", "Update-Windows.cmd")


def check_paths(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Распакуйте обновление в отдельную папку, вне прежней папки программы.")
    for root in (source, target):
        for relative in ("backend/app/cli.py", "backend/requirements.txt", "frontend/package.json"):
            if not (root / relative).is_file():
                raise ValueError("Выберите папку Novel Studio, в которой находятся backend и frontend.")
    python = target / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file() or not (target / "backend/.env").is_file():
        raise ValueError("В прежней папке не найдены .venv или backend/.env. Обновление остановлено.")
    return source, target, python


def source_files(source):
    for name in SOURCE_FILES:
        if (source / name).is_file():
            yield source / name
    for name in SOURCE_DIRS:
        root = source / name
        if not root.exists():
            continue
        for folder, directories, names in os.walk(root, followlinks=False):
            directories[:] = [d for d in directories if d not in SKIP_DIRS
                              and not (Path(folder) / d).is_symlink()]
            for name in names:
                path = Path(folder) / name
                if (path.is_symlink() or (name.startswith(".env") and name != ".env.example")
                        or name.endswith((".db", ".db-wal", ".db-shm", ".sqlite3", ".sqlite3-wal",
                                         ".sqlite3-shm", ".jks", ".keystore", ".pyc"))):
                    continue
                yield path


def copy_sources(source, target):
    for path in source_files(source):
        destination = target / path.relative_to(source)
        if any(p.is_symlink() for p in (destination, *destination.parents)):
            raise ValueError("В папке программы найдена ссылка; безопасное обновление остановлено.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def run(args, cwd):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True)


def ensure_stopped(python):
    if os.name != "nt":
        return
    script = r"""
$found = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
    $_.ProcessId -ne [int]$env:NOVEL_UPDATER_PID -and
    ($_.ExecutablePath -ieq $env:NOVEL_UPDATER_PYTHON -or
      ($_.Name -like 'python*' -and
       ([string]$_.CommandLine).IndexOf($env:NOVEL_UPDATER_PYTHON,
          [System.StringComparison]::OrdinalIgnoreCase) -ge 0))
}
if ($found) { exit 10 }
"""
    env = dict(os.environ, NOVEL_UPDATER_PID=str(os.getpid()), NOVEL_UPDATER_PYTHON=str(python))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("Остановите сервер и обработчик ИИ: нажмите Ctrl+C в обоих окнах PowerShell. "
                         "Если они уже остановлены, закройте их окна и повторите обновление.")


def update(source, target, runner=run):
    source, target, python = check_paths(source, target)
    ensure_stopped(python)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise ValueError("Node.js/npm не найден. Перезапустите PowerShell после установки Node.js.")
    for path in (target / "frontend/dist", target / "frontend"):
        if path.is_symlink():
            raise ValueError("Папка интерфейса содержит ссылку; обновление остановлено.")
    backup = target / "backend/data/backups" / (
        "before-update-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + "-" + uuid4().hex[:8] + ".db")
    print("1/5. Сохраняю резервную копию базы.", flush=True)
    runner([python, "-m", "app.cli", "backup", "--output", backup], target / "backend")
    if not backup.is_file() or backup.stat().st_size == 0:
        raise ValueError("Резервная копия не создана; исходники не изменены.")
    print("Резервная копия:", backup, flush=True)
    print("2/5. Подготавливаю новый интерфейс. Дождитесь завершения.", flush=True)
    runner([npm, "install"], source / "frontend")
    runner([npm, "run", "build"], source / "frontend")
    if not (source / "frontend/dist/index.html").is_file():
        raise ValueError("Интерфейс не собран; исходники прежней программы не изменены.")
    print("3/5. Обновляю зависимости.", flush=True)
    runner([python, "-m", "pip", "install", "-r", source / "backend/requirements.txt"], target)
    print("4/5. Обновляю исходники и схему базы.", flush=True)
    copy_sources(source, target)
    runner([python, "-m", "app.cli", "migrate"], target / "backend")
    print("5/5. Устанавливаю собранный интерфейс.", flush=True)
    shutil.copytree(source / "frontend/dist", target / "frontend/dist", dirs_exist_ok=True)
    print("Готово. Данные, ключ и учётная запись сохранены.", flush=True)
    print("Запускайте Novel Studio из прежней папки:", target, flush=True)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description="Обновление Novel Studio с резервной копией базы")
    parser.add_argument("--target", help="Прежняя папка Novel Studio")
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    entered = args.target or input("Вставьте путь прежней папки Novel Studio и нажмите Enter: ")
    try:
        update(source, Path(entered.strip().strip('"')))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("Обновление остановлено:", error, file=sys.stderr)
        print("Не удаляйте прежнюю папку и backend/data/backups. Скопируйте текст ошибки.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
