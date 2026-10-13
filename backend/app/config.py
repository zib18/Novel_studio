import os
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NOVEL_", env_file=str(Path(__file__).resolve().parents[1] / ".env"), extra="ignore")

    database_url: str = f"sqlite:///{(DATA_DIR / 'novel.db').as_posix()}"
    allowed_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:8000", "http://127.0.0.1:8000",
        "http://localhost:5173", "http://127.0.0.1:5173",
    ])
    public_url: str = ""
    cookie_secure: bool = False
    cookie_name: str = "novel_session"
    session_days: int = Field(default=30, ge=1, le=365)
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    ai_daily_budget_usd: float = Field(default=5.0, gt=0)
    ai_max_calls: int = Field(default=20, ge=1, le=100)
    ai_context_chars: int = Field(default=40000, ge=10000, le=250000)
    ai_max_output_tokens: int = Field(default=4500, ge=500, le=30000)
    ai_timeout_seconds: int = Field(default=120, ge=10, le=600)
    ai_job_budget_usd: float = Field(default=1.0, gt=0, le=100)
    ai_input_usd_per_million: float = Field(default=0.4, ge=0)
    ai_output_usd_per_million: float = Field(default=1.6, ge=0)

    @field_validator("database_url", mode="before")
    @classmethod
    def use_psycopg_driver(cls, value):
        # Hosted PostgreSQL connection strings omit the SQLAlchemy driver.
        if isinstance(value, str):
            if value.startswith("postgres://"):
                return "postgresql+psycopg://" + value[len("postgres://"):]
            if value.startswith("postgresql://"):
                return "postgresql+psycopg://" + value[len("postgresql://"):]
        return value

    @model_validator(mode="after")
    def configure_public_origin(self):
        external = self.public_url.strip() or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        if not external:
            return self
        parsed = urlsplit(external)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("NOVEL_PUBLIC_URL must be one HTTPS origin without credentials or a path")
        port = parsed.port  # also rejects invalid port values
        host = parsed.hostname.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = "[" + host + "]"
        origin = "https://" + host + ((":" + str(port)) if port not in (None, 443) else "")
        self.public_url = origin
        self.allowed_origins = [origin]
        self.cookie_secure = True
        return self
