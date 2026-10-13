"""Test the production container over trusted HTTPS, without OpenAI calls.

Uses only the Python standard library. The isolated smoke Compose project
is removed afterwards, including its disposable database volume.
"""
from __future__ import annotations
from dataclasses import dataclass
from html.parser import HTMLParser
import http.cookiejar
import json
import os
from pathlib import Path
import re
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4
from io import BytesIO
from zipfile import ZipFile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy" / "compose.smoke.yml"
PROJECT = "novel-deploy-smoke"
ORIGIN = "https://localhost:8443"
ARTIFACTS = ROOT / "deployment-artifacts"
EMAIL = "deployment-smoke@example.test"
PASSWORD = "Novel Studio disposable smoke password 2026!"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def redact(value: str) -> str:
    value = value.replace(PASSWORD, "[redacted]")
    value = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@", r"\1[redacted]@", value)
    return value


def run(args, *, label, timeout=180, input_text=None, check=True, env=None):
    print(label, flush=True)
    result = subprocess.run(args, cwd=ROOT, input=input_text, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    with (ARTIFACTS / "commands.log").open("a", encoding="utf-8") as log:
        log.write(f"\n{label}\n")
        log.write(redact(result.stdout))
        log.write(redact(result.stderr))
    if check and result.returncode:
        print(redact(result.stdout[-6000:] + result.stderr[-6000:]), flush=True)
        raise RuntimeError(f"{label} failed with exit code {result.returncode}; see deployment-artifacts/commands.log")
    return result


def compose(*args, label, **kwargs):
    return run(["docker", "compose", "--project-name", PROJECT, "--file", str(COMPOSE_FILE), *args],
               label=label, **kwargs)


def container_id(service):
    result = compose("ps", "--all", "--quiet", service, label=f"Find {service} container", timeout=20)
    identifiers = result.stdout.strip().splitlines()
    assert len(identifiers) == 1, f"Expected one {service} container."
    return identifiers[0]


def database_volume():
    result = run(["docker", "inspect", "--format", "{{json .Mounts}}", container_id("db")],
                 label="Inspect database volume", timeout=20)
    mounts = json.loads(result.stdout)
    volumes = [mount["Name"] for mount in mounts if mount["Type"] == "volume"
               and mount["Destination"].startswith("/var/lib/postgresql")]
    assert len(volumes) == 1, "PostgreSQL must use one persistent named data volume."
    return volumes[0]


def copy_caddy_ca(destination):
    identifier = container_id("tls")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = run(["docker", "cp", f"{identifier}:/data/caddy/pki/authorities/local/root.crt", str(destination)],
                     label="Copy public Caddy CA certificate", timeout=20, check=False)
        if result.returncode == 0 and destination.is_file():
            return
        time.sleep(0.5)
    raise RuntimeError("Caddy did not create its local CA certificate.")


@dataclass
class Response:
    status: int
    headers: object
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8-sig"))

    def text(self):
        return self.body.decode("utf-8-sig")

    def expect(self, status=200):
        assert self.status == status, f"Expected HTTP {status}, received HTTP {self.status}."
        return self


class Client:
    def __init__(self, context):
        self.jar = http.cookiejar.CookieJar()
        self.headers = {"Origin": ORIGIN}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context), urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, body=None, headers=None):
        url = urllib.parse.urljoin(ORIGIN + "/", path)
        assert urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(ORIGIN).netloc
        active_headers = dict(self.headers)
        active_headers.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            active_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=active_headers, method=method)
        try:
            response = self.opener.open(request, timeout=8)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            assert len(content) <= MAX_RESPONSE_BYTES, "Unexpected response size."
            return Response(response.status, response.headers, content)

    def json(self, method, path, body=None, *, status=200, headers=None):
        return self.request(method, path, body, headers).expect(status).json()


def wait_status(client, path="/api/ready", status=200, timeout=90):
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        try:
            response = client.request("GET", path)
            last_status = response.status
            if response.status == status:
                return response
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{path} did not reach HTTP {status}; last HTTP status: {last_status}.")


def assert_secure_cookie(response):
    cookies = response.headers.get_all("Set-Cookie") or []
    assert cookies, "Session response did not set a cookie."
    assert any(all(attribute in value.lower() for attribute in ("; secure", "; httponly", "samesite=lax"))
               for value in cookies), "Session cookie lacks required HTTPS attributes."


def login(client):
    initial_response = client.request("GET", "/api/v1/session").expect()
    assert_secure_cookie(initial_response)
    assert "no-store" in initial_response.headers.get("Cache-Control", "").lower()
    session = initial_response.json()
    client.headers["X-CSRF-Token"] = session["csrf_token"]
    response = client.request("POST", "/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD}).expect()
    assert_secure_cookie(response)
    session = client.json("GET", "/api/v1/session")
    assert session["user"]["email"] == EMAIL
    client.headers["X-CSRF-Token"] = session["csrf_token"]


class IndexParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            source = dict(attrs).get("src")
            if source:
                self.scripts.append(source)


def verify_static(client):
    index = client.request("GET", "/").expect()
    assert "text/html" in index.headers.get("Content-Type", "")
    assert "Novel Studio" in index.text()
    parser = IndexParser()
    parser.feed(index.text())
    scripts = [path for path in parser.scripts if urllib.parse.urlsplit(path).path.endswith(".js")]
    assert scripts, "Built application does not reference a JavaScript asset."
    script = client.request("GET", scripts[0]).expect()
    assert "javascript" in script.headers.get("Content-Type", "")
    assert script.body and not script.body.lstrip().startswith(b"<!doctype")
    manifest = client.json("GET", "/manifest.webmanifest")
    assert manifest["display"] == "standalone"
    assert manifest["start_url"]
    for size in (192, 512):
        icon = next(icon for icon in manifest["icons"] if f"{size}x{size}" in icon.get("sizes", "").split()
                    and icon.get("type") == "image/png")
        image = client.request("GET", icon["src"]).expect()
        assert image.body.startswith(b"\x89PNG\r\n\x1a\n")
        assert image.body[12:16] == b"IHDR"
        assert struct.unpack(">II", image.body[16:24]) == (size, size)
    worker = client.request("GET", "/sw.js").expect()
    assert "javascript" in worker.headers.get("Content-Type", "")
    assert "no-cache" in worker.headers.get("Cache-Control", "").lower()
    assert "addEventListener" in worker.text()
    client.request("GET", "/assets/smoke-missing.js").expect(404)
    client.request("GET", "/api/v1/smoke-missing").expect(404)


def create_author():
    # Use the actual Windows-style backup/CLI transfer route. The staging file
    # is root-owned mode 600, so a normal runtime user cannot read it.
    compose("stop", "api", label="Stop API for private database import", timeout=60)
    source = """
import json, os, sys
from pathlib import Path
from uuid import uuid4
from app.cli import migrate, create_user, backup
from app.config import Settings
from app.database import make_engine, build_session_factory
from app.models import Novel, Chapter, Revision, BibleEntry
from app.services import payload_hash
values = json.load(sys.stdin)
settings = Settings(database_url="sqlite:////transfer/work.db", openai_api_key="")
migrate(settings)
engine = make_engine(settings.database_url)
with build_session_factory(engine)() as db:
    user = create_user(db, values["email"], values["password"])
    novel = Novel(owner_id=user.id, title="Роман из Windows", genre="роман")
    db.add(novel)
    db.commit()
    chapter = Chapter(novel_id=novel.id, number=1, title="Сохранённая глава")
    db.add(chapter)
    db.commit()
    content = "Марина сохранила письмо. Эта ручная редакция перенесена из Windows."
    revision = Revision(chapter_id=chapter.id, text=content, source="manual",
                        mutation_id=str(uuid4()), payload_hash=payload_hash(None, content))
    db.add(revision)
    db.commit()
    chapter.current_revision_id = revision.id
    chapter.approved_revision_id = revision.id
    chapter.status = "APPROVED"
    db.add(BibleEntry(novel_id=novel.id, kind="character", title="Марина",
                     content="Героиню зовут Марина.", status="CANON"))
    db.commit()
    identifiers = {"novel": novel.id, "chapter": chapter.id, "revision": revision.id}
engine.dispose()
snapshot = backup(settings, Path("/transfer/novel-transfer.db"))
os.chmod(snapshot, 0o600)
print(json.dumps(identifiers))
"""
    with tempfile.TemporaryDirectory(prefix="novel-private-import-") as directory:
        prepared = compose(
            "run", "--rm", "--no-deps", "--user", "0:0",
            "-v", f"{directory}:/transfer", "api", "python", "-c", source,
            label="Create a consistent private SQLite snapshot",
            input_text=json.dumps({"email": EMAIL, "password": PASSWORD}), timeout=90,
        )
        identifiers = json.loads(prepared.stdout.strip().splitlines()[-1])
        snapshot = Path(directory) / "novel-transfer.db"
        assert snapshot.is_file()
        imported = compose(
            "run", "--rm", "--no-deps", "--user", "0:0",
            "-v", f"{snapshot}:/tmp/novel-transfer.db:ro",
            "api", "python", "-m", "app.transfer", "--source", "/tmp/novel-transfer.db",
            label="Import private backup using the real container CLI", timeout=90,
        )
        report = json.loads(imported.stdout.strip().splitlines()[-1])
        assert report["copied"]["users"] == 1
        assert report["copied"]["chapters"] == 1
        assert report["copied"]["revisions"] == 1
    compose("up", "-d", "api", label="Start API after import", timeout=180)
    return identifiers


def export(client, novel_id, file_format, selection):
    query = urllib.parse.urlencode({"format": file_format, "selection": selection})
    response = client.request("GET", f"/api/v1/novels/{novel_id}/export?{query}").expect()
    assert "attachment" in response.headers.get("Content-Disposition", "").lower()
    return response


def verify_database_clients(desktop, mobile):
    novel = desktop.json("POST", "/api/v1/novels",
            {"title": "Контейнерная проверка", "genre": "Современный роман"}, status=201)
    chapter = desktop.json("POST", f"/api/v1/novels/{novel['id']}/chapters", {"title": "Письмо", "number": 1}, status=201)
    path = f"/api/v1/chapters/{chapter['id']}"
    initial = mobile.json("GET", path)["current_revision"]["id"]
    approved_text = "Под вечер Вера вернулась к письму.\n\nВ последней строке обнаружилось неожиданное условие."
    payload = {"base_revision_id": initial, "text": approved_text, "mutation_id": str(uuid4())}
    saved = desktop.json("PUT", path + "/text", payload)["current_revision"]
    replay = desktop.json("PUT", path + "/text", payload)["current_revision"]
    assert replay["id"] == saved["id"]
    assert mobile.json("GET", path)["current_revision"]["text"] == approved_text
    mobile.request("PUT", path + "/text", {"base_revision_id": initial,
                   "text": "Устаревшая мобильная правка.", "mutation_id": str(uuid4())}).expect(409)
    assert desktop.json("GET", path)["current_revision"]["id"] == saved["id"]
    desktop.json("POST", path + "/approve", {"base_revision_id": saved["id"]})
    current_text = "На следующее утро Вера перечитала письмо.\n\nНовая ручная редакция оставалась её собственным решением."
    current = desktop.json("PUT", path + "/text", {"base_revision_id": saved["id"],
                           "text": current_text, "mutation_id": str(uuid4())})["current_revision"]
    assert current_text in export(desktop, novel["id"], "txt", "current").text()
    published = export(desktop, novel["id"], "txt", "approved").text()
    assert approved_text in published and current_text not in published
    document = export(desktop, novel["id"], "docx", "current")
    assert document.headers.get("Content-Type", "").startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    with ZipFile(BytesIO(document.body)) as archive:
        assert "[Content_Types].xml" in archive.namelist()
        xml = ET.fromstring(archive.read("word/document.xml"))
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    document_text = "\n".join("".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
            for paragraph in xml.findall(".//w:p", namespace))
    for paragraph in current_text.split("\n\n"):
        assert paragraph in document_text
    status = desktop.json("GET", "/api/v1/system/status")
    assert status["ai_configured"] is False
    desktop.request("POST", f"/api/v1/novels/{novel['id']}/jobs", {"chapter_id": chapter["id"],
        "base_revision_id": current["id"], "kind": "write", "instructions": "Продолжить главу."},
        {"Idempotency-Key": str(uuid4())}).expect(503)

    def assert_persisted():
        durable = mobile.json("GET", path)["current_revision"]
        assert durable["id"] == current["id"] and durable["text"] == current_text
        history = mobile.json("GET", path + "/revisions")
        assert history["total"] >= 3
        assert {saved["id"], current["id"]} <= {revision["id"] for revision in history["items"]}
        assert approved_text in export(desktop, novel["id"], "txt", "approved").text()
    assert_persisted()
    return assert_persisted


def verify_idle_worker():
    source = """
from sqlalchemy import select, func
from app.config import Settings
from app.database import make_engine
from app.generation.models import GenerationJob
engine = make_engine(Settings().database_url)
with engine.connect() as db:
    assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
engine.dispose()
"""
    compose("exec", "-T", "api", "python", "-c", source,
            label="Confirm no queued generation before worker smoke")
    compose("up", "-d", "worker", label="Start idle worker with a non-billable test key")
    time.sleep(5)
    result = run(["docker", "inspect", "--format", "{{json .State}}", container_id("worker")],
                 label="Inspect idle worker process", timeout=20)
    assert json.loads(result.stdout)["Running"] is True
    compose("stop", "worker", label="Stop idle smoke worker", timeout=30)


def verify(context):
    anonymous = Client(context)
    wait_status(anonymous)
    anonymous.request("GET", "/api/health").expect()
    anonymous.request("GET", "/api/v1/novels").expect(401)
    verify_static(anonymous)
    compose("exec", "-T", "api", "python", "-m", "app.cli", "migrate",
            label="Verify migrations can run again", timeout=60)
    imported = create_author()
    wait_status(anonymous)
    desktop, mobile = Client(context), Client(context)
    login(desktop)
    login(mobile)
    desktop.request("POST", "/api/v1/novels", {"title": "Forbidden origin"},
                    {"Origin": "https://attacker.invalid"}).expect(403)
    desktop.request("POST", "/api/v1/novels", {"title": "Forbidden CSRF"},
                    {"X-CSRF-Token": "invalid-smoke-token"}).expect(403)
    assert_persisted = verify_database_clients(desktop, mobile)
    original_volume = database_volume()
    verify_idle_worker()
    compose("stop", "db", label="Stop PostgreSQL", timeout=60)
    wait_status(anonymous, "/api/ready", 503, timeout=30)
    anonymous.request("GET", "/api/health").expect()
    compose("up", "-d", "db", label="Restore PostgreSQL", timeout=180)
    wait_status(anonymous)
    assert_persisted()
    compose("up", "-d", "--force-recreate", "db", label="Recreate PostgreSQL with its existing volume", timeout=180)
    wait_status(anonymous)
    assert database_volume() == original_volume
    assert_persisted()
    compose("up", "-d", "--force-recreate", "api", label="Recreate API container", timeout=180)
    wait_status(anonymous)
    assert_persisted()
    verify_static(anonymous)
    # Exercise the real proxy limiter after all session-dependent checks.
    throttled = False
    for attempt in range(20):
        response = anonymous.request("GET", "/api/v1/session")
        if response.status == 429:
            assert response.json()["detail"]["code"] == "rate_limited"
            assert response.headers.get("Retry-After")
            throttled = True
            break
        response.expect()
    assert throttled, "Public session endpoint must be rate limited."


def main():
    assert COMPOSE_FILE.is_file(), "deploy/compose.smoke.yml is missing."
    ARTIFACTS.mkdir(exist_ok=True)
    try:
        run(["docker", "compose", "--file", str(ROOT / "compose.yaml"), "config", "--quiet"],
            label="Validate production Compose", env=dict(os.environ, NOVEL_DOMAIN="novel.example.test",
            POSTGRES_PASSWORD="disposable-production-config-only", NOVEL_OPENAI_API_KEY=""))
        compose("config", "--quiet", label="Validate smoke Compose configuration")
        compose("down", "--volumes", "--remove-orphans", label="Remove previous disposable smoke project")
        compose("build", "api", label="Build production image", timeout=600)
        compose("up", "-d", "db", "api", "tls", label="Start isolated HTTPS deployment")
        with tempfile.TemporaryDirectory(prefix="novel-deployment-ca-") as directory:
            certificate = Path(directory) / "root.crt"
            copy_caddy_ca(certificate)
            context = ssl.create_default_context(cafile=str(certificate))
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            verify(context)
        print("Deployment passed: trusted HTTPS, secure sessions, CSRF, PWA assets, two clients, "
              "conflicts, TXT/DOCX, DB readiness and persistent volumes. No OpenAI generation was performed.")
    finally:
        try:
            logs = compose("logs", "--no-color", "--tail", "100",
                           label="Collect deployment logs", timeout=30, check=False)
            (ARTIFACTS / "compose.log").write_text(redact(logs.stdout + logs.stderr), encoding="utf-8")
            print(redact(logs.stdout[-14000:] + logs.stderr[-2000:]), flush=True)
        finally:
            compose("down", "--volumes", "--remove-orphans", label="Remove disposable smoke project",
                    timeout=90, check=False)


if __name__ == "__main__":
    main()
