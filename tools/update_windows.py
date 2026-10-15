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


def windows_processes():
    """Read process images directly; this avoids WMI/PowerShell service timeouts."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            result.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(error)
    finally:
        kernel.CloseHandle(snapshot)

    images = {}
    for pid, _, name in result:
        if not name.casefold().startswith("python"):
            continue
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # The process exited before OpenProcess.
                continue
            raise ValueError(f"Не удалось проверить процесс Python (PID {pid}, код {error}). "
                             "Обновление остановлено до изменения файлов.")
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                images[pid] = buffer.value
            else:
                error = ctypes.get_last_error()
                if error not in (6, 87):  # A process can disappear during enumeration.
                    raise ValueError(f"Не удалось прочитать сведения о Python (PID {pid}, код {error}). "
                                     "Обновление остановлено до изменения файлов.")
        finally:
            kernel.CloseHandle(handle)
    return result, images


def ensure_stopped(python):
    if os.name != "nt":
        return
    python = Path(python)
    processes, images = windows_processes()
    parents = {pid: parent for pid, parent, _ in processes}
    # A Windows venv redirector can be the waiting parent of this Python process.
    ancestors, pid = set(), os.getpid()
    while pid and pid not in ancestors:
        ancestors.add(pid)
        pid = parents.get(pid, 0)
    target_images = {os.path.normcase(str(python.resolve())),
                     os.path.normcase(str(python.with_name("pythonw.exe").resolve()))}
    if any(pid not in ancestors and os.path.normcase(image) in target_images
           for pid, image in images.items()):
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
