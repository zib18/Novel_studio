"""Reversible author-controlled trash; approved facts and rejection rules are retained."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from .auth import fail, get_current_user, require_csrf
from .database import get_db
from .models import AuthorDecision, BibleEntry, Chapter, Novel, utcnow
from .schemas import TrashAction
from .services import owned_novel, record_dict
from .generation.models import GenerationJob, utc_now

router = APIRouter(prefix="/api/v1", tags=["Корзина"])
PENDING = ("IDEA", "PROPOSAL", "UNDEFINED")
CANCELLABLE = ("queued", "running", "awaiting_author", "needs_author_review")


def _owned_any_novel(db, novel_id, user):
    changed = db.execute(update(Novel).where(
        Novel.id == novel_id, Novel.owner_id == user.id
    ).values(updated_at=Novel.updated_at).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        fail(404, "not_found", "Роман не найден")
    return db.scalar(select(Novel).where(
        Novel.id == novel_id, Novel.owner_id == user.id
    ).execution_options(populate_existing=True))


def _cancel_jobs(db, novel_id):
    return db.execute(update(GenerationJob).where(
        GenerationJob.novel_id == novel_id, GenerationJob.status.in_(CANCELLABLE)
    ).values(status="cancelled", stage="context_changed", cancel_requested=True,
             active_chapter_id=None, lease_owner=None, lease_until=None, updated_at=utc_now(),
             error="Контекст романа изменён. При необходимости запустите новую задачу."
             ).execution_options(synchronize_session=False)).rowcount


def _novel_view(db, novel):
    item = record_dict(novel)
    item["chapter_count"] = db.scalar(select(func.count(Chapter.id)).where(Chapter.novel_id == novel.id))
    return item


@router.get("/trash/novels")
def list_trashed_novels(db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = db.scalars(select(Novel).where(
        Novel.owner_id == user.id, Novel.deleted_at.is_not(None)
    ).order_by(Novel.deleted_at.desc())).all()
    return {"items": [_novel_view(db, novel) for novel in rows], "total": len(rows)}


@router.get("/novels/{novel_id}/trash/ideas")
def list_trashed_ideas(novel_id: str, limit: int = Query(100, ge=1, le=100),
                      offset: int = Query(0, ge=0), db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    where = (BibleEntry.novel_id == novel_id, BibleEntry.deleted_at.is_not(None))
    rows = db.scalars(select(BibleEntry).where(*where).order_by(
        BibleEntry.deleted_at.desc(), BibleEntry.id).limit(limit).offset(offset)).all()
    return {"items": [record_dict(entry) for entry in rows],
            "total": db.scalar(select(func.count(BibleEntry.id)).where(*where))}


def _transition_novel(db, user, novel_id, body, restoring):
    novel = _owned_any_novel(db, novel_id, user)
    if (novel.deleted_at is None) == restoring:
        fail(409, "invalid_trash_state", "Роман уже восстановлен" if restoring else "Роман уже в корзине")
    state = Novel.deleted_at.is_not(None) if restoring else Novel.deleted_at.is_(None)
    changed = db.execute(update(Novel).where(
        Novel.id == novel.id, Novel.owner_id == user.id, state,
        Novel.version == body.base_version, Novel.canon_version == body.expected_canon_version
    ).values(deleted_at=None if restoring else utcnow(), version=Novel.version + 1,
             canon_version=Novel.canon_version + 1, updated_at=utcnow()
             ).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.rollback()
        fail(409, "novel_conflict", "Роман изменился. Обновите список и повторите действие")
    cancelled = _cancel_jobs(db, novel.id)
    db.add(AuthorDecision(novel_id=novel.id, user_id=user.id,
                          action="restore_novel" if restoring else "trash_novel",
                          payload={"title": novel.title, "previous_version": body.base_version,
                                   "canon_version": body.expected_canon_version + 1}))
    db.commit()
    db.refresh(novel)
    return {**_novel_view(db, novel), "cancelled_jobs": cancelled}


@router.post("/novels/{novel_id}/trash", dependencies=[Depends(require_csrf)])
def trash_novel(novel_id: str, body: TrashAction, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    return _transition_novel(db, user, novel_id, body, False)


@router.post("/novels/{novel_id}/restore", dependencies=[Depends(require_csrf)])
def restore_novel(novel_id: str, body: TrashAction, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    return _transition_novel(db, user, novel_id, body, True)


def _transition_idea(db, user, entry_id, body, restoring):
    entry = db.get(BibleEntry, entry_id)
    if entry is None:
        fail(404, "not_found", "Запись не найдена")
    novel = owned_novel(db, entry.novel_id, user)
    db.refresh(entry)
    if entry.status not in PENDING or not entry.active:
        fail(422, "protected_entry", "Удалять можно только неутверждённые идеи и предложения")
    if (entry.deleted_at is None) == restoring:
        fail(409, "invalid_trash_state", "Запись уже восстановлена" if restoring else "Запись уже в корзине")
    state = BibleEntry.deleted_at.is_not(None) if restoring else BibleEntry.deleted_at.is_(None)
    changed_novel = db.execute(update(Novel).where(
        Novel.id == novel.id, Novel.deleted_at.is_(None),
        Novel.canon_version == body.expected_canon_version
    ).values(version=Novel.version + 1, canon_version=Novel.canon_version + 1, updated_at=utcnow()
             ).execution_options(synchronize_session=False)).rowcount
    changed_entry = db.execute(update(BibleEntry).where(
        BibleEntry.id == entry.id, BibleEntry.novel_id == novel.id, state,
        BibleEntry.version == body.base_version, BibleEntry.active.is_(True),
        BibleEntry.status.in_(PENDING)
    ).values(deleted_at=None if restoring else utcnow(), version=BibleEntry.version + 1
             ).execution_options(synchronize_session=False)).rowcount
    if changed_novel != 1 or changed_entry != 1:
        db.rollback()
        fail(409, "canon_conflict", "Запись или канон изменились. Обновите список перед повтором")
    cancelled = _cancel_jobs(db, novel.id)
    db.add(AuthorDecision(novel_id=novel.id, user_id=user.id, bible_entry_id=entry.id,
                          action="restore_idea" if restoring else "trash_idea",
                          payload={"title": entry.title, "status": entry.status,
                                   "previous_version": body.base_version,
                                   "canon_version": body.expected_canon_version + 1}))
    db.commit()
    db.refresh(novel)
    db.refresh(entry)
    return {"entry": record_dict(entry), "novel": record_dict(novel),
            "canon_version": novel.canon_version, "cancelled_jobs": cancelled}


@router.post("/bible/{entry_id}/trash", dependencies=[Depends(require_csrf)])
def trash_idea(entry_id: str, body: TrashAction, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    return _transition_idea(db, user, entry_id, body, False)


@router.post("/bible/{entry_id}/restore", dependencies=[Depends(require_csrf)])
def restore_idea(entry_id: str, body: TrashAction, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    return _transition_idea(db, user, entry_id, body, True)
