"""Application factory; schema changes are explicit Alembic migrations."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import make_engine
from app.auth import get_current_user
from app.api import router as core_router
from app.exports import router as export_router
from app.generation.api import router as generation_router
from app import models  # register core tables
from app.generation import models as generation_models  # register task tables

VERSION = "0.2.0"
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(application):
        yield
        engine.dispose()

    application = FastAPI(title="Novel Studio", version=VERSION, lifespan=lifespan)
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
    )

    @application.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/api/health")
    def health():
        # Liveness only: does not assert DB migration, API key or worker readiness.
        return {"status": "ok", "service": "Novel Studio", "version": VERSION, "stage": "core"}

    @application.get("/api/ready")
    def readiness():
        # Checks database and both schema generations; no paid AI call.
        try:
            with engine.connect() as connection:
                connection.execute(select(models.User.id).limit(1))
                connection.execute(select(generation_models.GenerationJob.id).limit(1))
        except SQLAlchemyError:
            raise HTTPException(503, detail={"code": "database_not_ready"})
        return {"status": "ready", "database": "ok", "ai_access_checked": False}

    @application.get("/api/v1/system/status")
    def system_status(user=Depends(get_current_user)):
        key = settings.openai_api_key
        value = key.get_secret_value() if hasattr(key, "get_secret_value") else key
        return {
            "ai_configured": bool(value),
            "model": settings.openai_model,
            "job_budget_usd": settings.ai_job_budget_usd,
            "daily_budget_usd": settings.ai_daily_budget_usd,
            "input_price_per_million": settings.ai_input_usd_per_million,
            "output_price_per_million": settings.ai_output_usd_per_million,
            "prices_are_estimates": True,
        }

    application.include_router(core_router)
    application.include_router(generation_router)
    application.include_router(export_router)

    if DIST.is_dir():
        @application.get("/{path:path}", include_in_schema=False)
        def frontend(path: str):
            if path == "api" or path.startswith("api/"):
                raise HTTPException(404, "API route not found")
            target = (DIST / path).resolve()
            if not target.is_relative_to(DIST.resolve()):
                raise HTTPException(404)
            if not target.is_file():
                # Missing assets must not receive HTML disguised as JS.
                if "." in Path(path).name:
                    raise HTTPException(404)
                target = DIST / "index.html"
            cache = "no-cache" if target.name in {"index.html", "sw.js", "manifest.webmanifest"} else "public, max-age=3600"
            return FileResponse(target, headers={"Cache-Control": cache})

    return application


app = create_app()
