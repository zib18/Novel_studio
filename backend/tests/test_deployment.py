import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.cli import migrate
from app.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def no_hosting_environment(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.delenv("NOVEL_PUBLIC_URL", raising=False)


def test_public_origin_protects_cookies_and_csrf_origin():
    settings = Settings(public_url="https://NOVEL.example:443/", allowed_origins=["http://localhost:8000"])
    assert settings.public_url == "https://novel.example"
    assert settings.allowed_origins == ["https://novel.example"]
    assert settings.cookie_secure is True


def test_render_origin_without_storing_it_in_git(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://novel-studio.onrender.com")
    settings = Settings()
    assert settings.public_url == "https://novel-studio.onrender.com"
    assert settings.allowed_origins == [settings.public_url]
    assert settings.cookie_secure is True


@pytest.mark.parametrize("value", [
    "http://novel.example", "https://user:pass@novel.example", "https://novel.example/api",
    "https://novel.example?token=secret", "https://novel.example#fragment", "https://",
    "https://novel.example:bad",
])
def test_invalid_public_origins_fail_closed(value):
    with pytest.raises(ValidationError):
        Settings(public_url=value)


@pytest.mark.parametrize("source", ["postgres://user:pass@db/novel", "postgresql://user:pass@db/novel"])
def test_hosted_postgres_uses_installed_driver(source):
    assert Settings(database_url=source).database_url == "postgresql+psycopg://user:pass@db/novel"


def test_liveness_does_not_hide_missing_database_migrations(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'ready.db').as_posix()}")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        failure = client.get("/api/ready")
        assert failure.status_code == 503
        assert failure.json() == {"detail": {"code": "database_not_ready"}}
        migrate(settings)
        ready = client.get("/api/ready")
        assert ready.status_code == 200
        assert ready.json()["ai_access_checked"] is False
