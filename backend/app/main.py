from fastapi import FastAPI
from pydantic import BaseModel


APP_VERSION = "0.1.0"


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    stage: str


app = FastAPI(
    title="Novel Studio",
    description="Личная студия для работы с длинными веб-новеллами.",
    version=APP_VERSION,
)


@app.get(
    "/api/health",
    response_model=HealthResponse,
    tags=["Система"],
)
def health() -> HealthResponse:
    """Проверка запуска сервера, без проверки БД и ИИ."""
    return HealthResponse(
        status="ok",
        service="Novel Studio",
        version=APP_VERSION,
        stage="bootstrap",
    )
