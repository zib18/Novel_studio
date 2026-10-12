import hashlib
import json
from copy import deepcopy
from uuid import uuid4
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from app.database import get_db
from app.auth import get_current_user, require_csrf
from app.services import owned_novel, owned_chapter, save_revision
from app.models import Novel
from .context import AGENTS, make_snapshot, prompt_snapshot
from .models import GenerationJob, PromptVersion, utc_now
from .schemas import JobCreate, ApplyJob, PromptUpdate

router = APIRouter(prefix="/api/v1", tags=["Генерация и инструкции"])


def job_view(job):
    return {name: getattr(job, name) for name in
            ("id", "novel_id", "chapter_id", "base_revision_id", "canon_version", "kind",
             "instructions", "status", "stage", "review_passes", "max_review_passes",
             "artifacts", "usage", "error", "cancel_requested", "created_at", "updated_at")}


def owned_job(db, job_id, user):
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена.")
    owned_novel(db, job.novel_id, user)
    return job


@router.post("/novels/{novel_id}/jobs", dependencies=[Depends(require_csrf)])
def create_job(novel_id: str, body: JobCreate, request: Request,
               idempotency_key: str = Header(alias="Idempotency-Key"),
               db=Depends(get_db), user=Depends(get_current_user)):
    novel = owned_novel(db, novel_id, user)
    if not 8 <= len(idempotency_key) <= 100:
        raise HTTPException(422, "Idempotency-Key должен содержать от 8 до 100 символов.")
    payload_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(GenerationJob).where(GenerationJob.novel_id == novel_id,
                                                    GenerationJob.idempotency_key == idempotency_key))
    if existing:
        if existing.request_hash != payload_hash:
            raise HTTPException(409, "Ключ повтора уже использован для другого запроса.")
        return job_view(existing)
    chapter = owned_chapter(db, body.chapter_id, user)
    if chapter.novel_id != novel_id:
        raise HTTPException(404, "Глава не принадлежит роману.")
    if chapter.current_revision_id != body.base_revision_id:
        raise HTTPException(409, {"message": "Сначала синхронизируйте последнюю редакцию.",
                                  "current_revision_id": chapter.current_revision_id})
    settings = request.app.state.settings
    key = settings.openai_api_key
    if hasattr(key, "get_secret_value"):
        key = key.get_secret_value()
    if not key:
        raise HTTPException(503, "На сервере не настроен NOVEL_OPENAI_API_KEY. Генерация не запускалась.")
    if body.kind == "review" and not chapter.current_revision_id:
        raise HTTPException(422, "Для проверки сначала сохраните текст главы.")
    budget = body.budget_usd or (novel.settings or {}).get("job_budget_usd") or settings.ai_job_budget_usd
    budget = min(float(budget), settings.ai_job_budget_usd)
    snapshot = make_snapshot(db, novel, chapter, body.instructions, settings.ai_context_chars)
    snapshot["commercial_report"] = body.commercial_report
    snapshot["owner_id"] = user.id
    job = GenerationJob(id=str(uuid4()), novel_id=novel_id, chapter_id=chapter.id,
                        active_chapter_id=chapter.id, base_revision_id=body.base_revision_id,
                        canon_version=novel.canon_version, kind=body.kind, instructions=body.instructions,
                        max_review_passes=body.max_review_passes, idempotency_key=idempotency_key,
                        request_hash=payload_hash, snapshot=snapshot,
                        usage={"budget_usd": budget, "cost_usd": 0, "calls": [],
                               "prices_approximate": True, "model": settings.openai_model,
                               "daily_limit_usd": settings.ai_daily_budget_usd},
                        artifacts={})
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(GenerationJob).where(GenerationJob.novel_id == novel_id,
                                                       GenerationJob.idempotency_key == idempotency_key))
        if existing and existing.request_hash == payload_hash:
            return job_view(existing)
        raise HTTPException(409, "Для этой главы уже выполняется задача или ключ повтора занят.")
    db.refresh(job)
    return job_view(job)


@router.get("/novels/{novel_id}/jobs")
def list_jobs(novel_id: str, offset: int = 0, limit: int = 30,
              db=Depends(get_db), user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    offset, limit = max(0, offset), min(100, max(1, limit))
    query = select(GenerationJob).where(GenerationJob.novel_id == novel_id)
    total = db.scalar(select(func.count()).select_from(GenerationJob).where(GenerationJob.novel_id == novel_id))
    rows = db.scalars(query.order_by(GenerationJob.created_at.desc()).offset(offset).limit(limit))
    return {"items": [job_view(j) for j in rows], "total": total}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    return job_view(owned_job(db, job_id, user))


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_csrf)])
def cancel_job(job_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    job = owned_job(db, job_id, user)
    job.cancel_requested = True
    if job.status == "queued":
        job.status = "cancelled"
        job.active_chapter_id = None
    job.updated_at = utc_now()
    db.commit()
    return job_view(job)


@router.post("/jobs/{job_id}/reject", dependencies=[Depends(require_csrf)])
def reject_job(job_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    job = owned_job(db, job_id, user)
    if job.status not in ("awaiting_author", "needs_author_review", "failed"):
        raise HTTPException(409, "Задача ещё не завершена.")
    artifacts = deepcopy(job.artifacts)
    artifacts["author_decision"] = "rejected"
    changed = db.execute(update(GenerationJob).where(
        GenerationJob.id == job.id,
        GenerationJob.status.in_(("awaiting_author", "needs_author_review", "failed"))
    ).values(artifacts=artifacts, status="rejected", updated_at=utc_now())
      .execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.rollback()
        raise HTTPException(409, "По задаче уже принято другое решение. Обновите результат.")
    db.commit()
    db.refresh(job)
    return job_view(job)


@router.post("/jobs/{job_id}/apply", dependencies=[Depends(require_csrf)])
def apply_job(job_id: str, body: ApplyJob, db=Depends(get_db), user=Depends(get_current_user)):
    job = owned_job(db, job_id, user)
    novel = owned_novel(db, job.novel_id, user)
    chapter = owned_chapter(db, job.chapter_id, user)
    if job.status not in ("awaiting_author", "needs_author_review"):
        raise HTTPException(409, "Нет завершённого предложения для применения.")
    if job.artifacts.get("author_decision") == "rejected":
        raise HTTPException(409, "Предложение отклонено автором.")
    if novel.canon_version != job.canon_version:
        raise HTTPException(409, "Канон изменился после запуска. Перепроверьте текст с новым каноном.")
    if body.base_revision_id != job.base_revision_id:
        raise HTTPException(409, "Основа предложения отличается от последней редакции.")
    critical = any(f["severity"] == "critical" and f["status"] == "open"
                   for f in job.artifacts.get("findings", []))
    if (critical or not job.artifacts.get("check_complete")) and not body.acknowledge_critical:
        raise HTTPException(409, "Остались критические нарушения или неполная проверка. Требуется явное решение автора.")
    candidate = job.artifacts.get("revised_text") or job.artifacts.get("draft_text")
    if not candidate:
        raise HTTPException(409, "Нет сохранённого текста.")
    locked = db.execute(update(Novel).where(
        Novel.id == job.novel_id, Novel.canon_version == job.canon_version
    ).values(canon_version=Novel.canon_version)).rowcount
    if locked != 1:
        db.rollback()
        raise HTTPException(409, "Канон изменился после запуска. Перепроверьте текст.")
    revision = save_revision(db, chapter, body.base_revision_id, candidate, "ai", body.mutation_id, commit=False)
    artifacts = deepcopy(job.artifacts)
    artifacts["author_decision"] = "accepted"
    artifacts["applied_revision_id"] = revision.id
    changed = db.execute(update(GenerationJob).where(
        GenerationJob.id == job.id,
        GenerationJob.status.in_(("awaiting_author", "needs_author_review")),
        GenerationJob.cancel_requested.is_(False)
    ).values(artifacts=artifacts, status="applied", updated_at=utc_now())
      .execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.rollback()
        raise HTTPException(409, "Результат отменён, отклонён или уже применён. Обновите задачу.")
    db.commit()
    db.refresh(job)
    return {"revision": {"id": revision.id, "chapter_id": revision.chapter_id, "text": revision.text},
            "job": job_view(job)}


@router.get("/novels/{novel_id}/prompts")
def list_prompts(novel_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    prompts = prompt_snapshot(db, novel_id)
    return {"items": [{"agent": agent, **value} for agent, value in prompts.items()],
            "total": len(prompts)}


@router.put("/novels/{novel_id}/prompts/{agent}", dependencies=[Depends(require_csrf)])
def update_prompt(novel_id: str, agent: str, body: PromptUpdate,
                  db=Depends(get_db), user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    if agent not in AGENTS:
        raise HTTPException(404, "Неизвестный агент.")
    current = db.scalar(select(PromptVersion).where(PromptVersion.novel_id == novel_id,
                                                  PromptVersion.agent == agent)
                        .order_by(PromptVersion.version.desc()).limit(1))
    version = current.version if current else 0
    if body.base_version != version:
        raise HTTPException(409, "Инструкция изменена в другой сессии.")
    row = PromptVersion(novel_id=novel_id, agent=agent, version=version + 1, text=body.text)
    db.add(row)
    db.execute(update(Novel).where(Novel.id == novel_id).values(
        canon_version=Novel.canon_version + 1))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Инструкция изменена в другой сессии.")
    return {"agent": agent, "text": row.text, "version": row.version}
