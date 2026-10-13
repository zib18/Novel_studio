"""Container entry point. No credentials or database URL are printed."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

BACKEND = Path("/app/backend")
HERE = Path(__file__).resolve().parent


def database_ready():
    from app.config import Settings
    from app.database import make_engine
    from app.models import User
    from app.generation.models import GenerationJob
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError
    engine = make_engine(Settings().database_url)
    try:
        with engine.connect() as connection:
            connection.execute(select(User.id).limit(1))
            connection.execute(select(GenerationJob.id).limit(1))
        return True
    except SQLAlchemyError:
        return False
    finally:
        engine.dispose()


def worker():
    # Only the API applies migrations. Wait for schema without racing its DDL.
    for attempt in range(120):
        if database_ready():
            os.execv(sys.executable, [sys.executable, "-m", "app.generation.worker"])
        time.sleep(1)
    raise RuntimeError("Database migrations are not ready; check the API service")


def serve():
    port = int(os.environ.get("PORT", "10000"))
    if not 1024 <= port <= 65535 or port == 8001:
        raise ValueError("PORT must be a non-privileged port other than 8001")
    subprocess.run([sys.executable, "-m", "app.cli", "migrate"], cwd=BACKEND, check=True)
    config = Path("/tmp/novel-nginx.conf")
    config.write_text((HERE / "nginx.conf.template").read_text().replace("__PORT__", str(port)))
    subprocess.run(["nginx", "-e", "/dev/stderr", "-t", "-c", str(config)], check=True)
    children = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children.append(subprocess.Popen([
            sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
            "--port", "8001", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1",
        ], cwd=BACKEND))
        children.append(subprocess.Popen(["nginx", "-e", "/dev/stderr", "-c", str(config), "-g", "daemon off;"]))
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(0.25)
        if not stopping:
            raise RuntimeError("Application or reverse proxy stopped unexpectedly")
    finally:
        stop(None, None)
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def main():
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    role = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if role == "serve":
        serve()
    elif role == "worker":
        worker()
    else:
        raise ValueError("Use serve or worker")


if __name__ == "__main__":
    main()
