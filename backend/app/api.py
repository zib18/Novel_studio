from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .auth import (create_session, fail, get_current_user, normalize_email, public_user,
                   require_csrf, session_from_request, verify_password)
from .database import get_db
from .models import AuthorDecision, BibleEntry, Chapter, Novel, RejectRule, Revision, User, uid, utcnow
from .schemas import (BibleCreate, BibleDecision, BibleReplacement, ChapterApproval, ChapterCreate,
                      Login, NovelCreate, NovelPatch, NovelSettings, RejectionCreate, Restore, SaveText)
from .services import (APPROVED, chapter_detail, confirm_entry, owned_chapter, owned_entry,
                       owned_novel, payload_hash, record_dict, revision_meta, save_revision)

router = APIRouter(prefix="/api/v1")


@router.get("/session")
def get_session(request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    session = session_from_request(request, db)
    if session is None:
        session = create_session(request, response, db)
    user = db.get(User, session.user_id) if session.user_id else None
    return {"user": public_user(user), "csrf_token": session.csrf_token}


@router.post("/auth/login")
def login(body: Login, request: Request, response: Response,
          session=Depends(require_csrf), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == normalize_email(body.email)))
    dummy = "$".join(("pbkdf2_sha256", "600000", "00" * 16, "00" * 32))
    valid = verify_password(body.password, user.password_hash if user else dummy)
    if not user or not valid:
        fail(401, "invalid_credentials", "Неверный адрес или пароль")
    db.delete(session)
    session = create_session(request, response, db, user.id)
    return {"user": public_user(user), "csrf_token": session.csrf_token}


@router.post("/auth/logout")
def logout(request: Request, response: Response, session=Depends(require_csrf),
           db: Session = Depends(get_db)):
    db.delete(session)
    session = create_session(request, response, db)
    return {"user": None, "csrf_token": session.csrf_token}


@router.post("/novels", status_code=201, dependencies=[Depends(require_csrf)])
def create_novel(body: NovelCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    novel = Novel(owner_id=user.id, title=body.title, genre=body.genre, concept=body.concept,
                  settings=body.settings.model_dump())
    db.add(novel)
    db.commit()
    return record_dict(novel)


@router.get("/novels")
def list_novels(db: Session = Depends(get_db), user=Depends(get_current_user)):
    novels = db.scalars(select(Novel).where(Novel.owner_id == user.id, Novel.deleted_at.is_(None)).order_by(Novel.updated_at.desc()))
    result = []
    for novel in novels:
        item = record_dict(novel)
        item["chapter_count"] = db.scalar(select(func.count(Chapter.id)).where(Chapter.novel_id == novel.id))
        result.append(item)
    return {"items": result, "total": len(result)}


@router.get("/novels/{novel_id}")
def get_novel(novel_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return record_dict(owned_novel(db, novel_id, user))


@router.patch("/novels/{novel_id}", dependencies=[Depends(require_csrf)])
def patch_novel(novel_id: str, body: NovelPatch, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    novel = owned_novel(db, novel_id, user)
    values = body.model_dump(exclude_unset=True, exclude={"base_version"})
    if any(value is None for value in values.values()):
        fail(422, "null_field", "Поля романа не могут иметь значение null")
    if "settings" in values:
        merged = {**NovelSettings().model_dump(), **novel.settings, **values["settings"]}
        try:
            values["settings"] = NovelSettings.model_validate(merged).model_dump()
        except ValidationError as error:
            fail(422, "invalid_settings", "Настройки не согласованы", errors=[
                {"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
                for item in error.errors()
            ])
    changed = db.execute(update(Novel).where(Novel.id == novel.id, Novel.version == body.base_version)
                         .values(**values, version=Novel.version + 1,
                                 canon_version=Novel.canon_version + 1, updated_at=utcnow())
                         .execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.rollback()
        fail(409, "novel_conflict", "Настройки романа уже изменены")
    db.commit()
    db.refresh(novel)
    return record_dict(novel)


@router.get("/novels/{novel_id}/chapters")
def list_chapters(novel_id: str, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
                  db: Session = Depends(get_db), user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    where = Chapter.novel_id == novel_id
    chapters = db.scalars(select(Chapter).where(where).order_by(Chapter.number).limit(limit).offset(offset))
    return {"items": [record_dict(chapter) for chapter in chapters],
            "total": db.scalar(select(func.count(Chapter.id)).where(where))}


@router.post("/novels/{novel_id}/chapters", status_code=201, dependencies=[Depends(require_csrf)])
def create_chapter(novel_id: str, body: ChapterCreate, db: Session = Depends(get_db),
                   user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    number = body.number or (db.scalar(select(func.max(Chapter.number)).where(Chapter.novel_id == novel_id)) or 0) + 1
    chapter = Chapter(id=uid(), novel_id=novel_id, number=number, title=body.title)
    try:
        db.add(chapter)
        db.flush()
        revision = Revision(id=uid(), chapter_id=chapter.id, text="", source="initial",
                            mutation_id=uid(), payload_hash=payload_hash(None, ""))
        db.add(revision)
        db.flush()
        chapter.current_revision_id = revision.id
        db.commit()
    except IntegrityError:
        db.rollback()
        fail(409, "chapter_number_conflict", "Глава с таким номером уже существует")
    return chapter_detail(db, chapter)


@router.get("/chapters/{chapter_id}")
def get_chapter(chapter_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return chapter_detail(db, owned_chapter(db, chapter_id, user))


@router.put("/chapters/{chapter_id}/text", dependencies=[Depends(require_csrf)])
def put_text(chapter_id: str, body: SaveText, db: Session = Depends(get_db), user=Depends(get_current_user)):
    chapter = owned_chapter(db, chapter_id, user)
    revision = save_revision(db, chapter, body.base_revision_id, body.text, "manual", body.mutation_id)
    result = chapter_detail(db, chapter)
    result["saved_revision_id"] = revision.id
    result["saved_revision"] = record_dict(revision, ("payload_hash",))
    return result


@router.get("/chapters/{chapter_id}/revisions")
def list_revisions(chapter_id: str, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    owned_chapter(db, chapter_id, user)
    where = Revision.chapter_id == chapter_id
    rows = db.scalars(select(Revision).where(where).order_by(Revision.created_at.desc())
                      .limit(limit).offset(offset))
    return {"items": [revision_meta(row) for row in rows],
            "total": db.scalar(select(func.count(Revision.id)).where(where))}


@router.get("/revisions/{revision_id}")
def get_revision(revision_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    revision = db.get(Revision, revision_id)
    if revision is None:
        fail(404, "not_found", "Редакция не найдена")
    owned_chapter(db, revision.chapter_id, user)
    return record_dict(revision, ("payload_hash",))


@router.post("/chapters/{chapter_id}/restore", dependencies=[Depends(require_csrf)])
def restore(chapter_id: str, body: Restore, db: Session = Depends(get_db), user=Depends(get_current_user)):
    chapter = owned_chapter(db, chapter_id, user)
    old = db.get(Revision, body.revision_id)
    if not old or old.chapter_id != chapter.id:
        fail(404, "not_found", "Редакция не найдена")
    saved = save_revision(db, chapter, body.base_revision_id, old.text, "restore", body.mutation_id)
    result = chapter_detail(db, chapter)
    result["saved_revision_id"] = saved.id
    result["saved_revision"] = record_dict(saved, ("payload_hash",))
    return result


@router.post("/chapters/{chapter_id}/approve", dependencies=[Depends(require_csrf)])
def approve_chapter(chapter_id: str, body: ChapterApproval, db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    chapter = owned_chapter(db, chapter_id, user)
    revision = db.get(Revision, body.base_revision_id)
    if not revision or revision.chapter_id != chapter.id or not revision.text.strip():
        fail(422, "empty_chapter", "Нельзя утвердить пустую или чужую редакцию")
    db.execute(update(Novel).where(Novel.id == chapter.novel_id)
               .values(canon_version=Novel.canon_version + 1, updated_at=utcnow()))
    changed = db.execute(update(Chapter).where(
        Chapter.id == chapter.id, Chapter.current_revision_id == body.base_revision_id
    ).values(status="APPROVED", approved_revision_id=body.base_revision_id)
                         .execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.rollback()
        fail(409, "revision_conflict", "Перед утверждением загрузите последнюю редакцию")
    db.add(AuthorDecision(novel_id=chapter.novel_id, user_id=user.id, chapter_id=chapter.id,
                          action="approve_chapter", payload={"revision_id": body.base_revision_id}))
    db.commit()
    return chapter_detail(db, chapter)


@router.get("/novels/{novel_id}/bible")
def list_bible(novel_id: str, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    where = (BibleEntry.novel_id == novel_id) & BibleEntry.deleted_at.is_(None)
    entries = db.scalars(select(BibleEntry).where(where).order_by(BibleEntry.created_at)
                         .limit(limit).offset(offset))
    return {"items": [record_dict(entry) for entry in entries],
            "total": db.scalar(select(func.count(BibleEntry.id)).where(where))}


@router.post("/novels/{novel_id}/bible", status_code=201, dependencies=[Depends(require_csrf)])
def create_bible_entry(novel_id: str, body: BibleCreate, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    entry = BibleEntry(novel_id=novel_id, **body.model_dump())
    db.add(entry)
    db.commit()
    return record_dict(entry)


@router.post("/bible/{entry_id}/decision", dependencies=[Depends(require_csrf)])
def decide(entry_id: str, body: BibleDecision, db: Session = Depends(get_db),
           user=Depends(get_current_user)):
    entry = owned_entry(db, entry_id, user)
    return confirm_entry(db, entry, owned_novel(db, entry.novel_id, user), user, body)


@router.post("/bible/{entry_id}/replacement", status_code=201, dependencies=[Depends(require_csrf)])
def replacement(entry_id: str, body: BibleReplacement, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    old = owned_entry(db, entry_id, user)
    if old.version != body.base_version or not old.active:
        fail(409, "entry_conflict", "Запись уже изменена")
    if old.status not in APPROVED:
        fail(422, "not_approved", "Заменять можно только утверждённые факты")
    entry = BibleEntry(novel_id=old.novel_id, kind=old.kind, title=body.title, content=body.content,
                       status="PROPOSAL", supersedes_id=old.id,
                       details=body.details if body.details is not None else old.details,
                       available_from_chapter=body.available_from_chapter
                       if "available_from_chapter" in body.model_fields_set else old.available_from_chapter)
    db.add(entry)
    db.commit()
    return record_dict(entry)


@router.get("/novels/{novel_id}/rejections")
def list_rejections(novel_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    rows = list(db.scalars(select(RejectRule).where(RejectRule.novel_id == novel_id)
                          .order_by(RejectRule.created_at)))
    return {"items": [record_dict(row) for row in rows], "total": len(rows)}


@router.post("/novels/{novel_id}/rejections", status_code=201, dependencies=[Depends(require_csrf)])
def create_rejection(novel_id: str, body: RejectionCreate, db: Session = Depends(get_db),
                     user=Depends(get_current_user)):
    novel = owned_novel(db, novel_id, user)
    if body.scope:
        chapter = owned_chapter(db, body.scope, user)
        if chapter.novel_id != novel.id:
            fail(422, "scope_mismatch", "Область запрета должна принадлежать этому роману")
    decision = AuthorDecision(id=uid(), novel_id=novel_id, user_id=user.id,
                              action="forbid", reason=body.description, payload=body.model_dump())
    db.add(decision)
    db.flush()
    rule = RejectRule(novel_id=novel_id, source_decision_id=decision.id, **body.model_dump())
    db.add(rule)
    db.execute(update(Novel).where(Novel.id == novel_id).values(canon_version=Novel.canon_version + 1))
    db.commit()
    return record_dict(rule)


@router.get("/novels/{novel_id}/decisions")
def list_decisions(novel_id: str, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    owned_novel(db, novel_id, user)
    where = AuthorDecision.novel_id == novel_id
    rows = db.scalars(select(AuthorDecision).where(where).order_by(AuthorDecision.created_at.desc())
                      .limit(limit).offset(offset))
    return {"items": [record_dict(row) for row in rows],
            "total": db.scalar(select(func.count(AuthorDecision.id)).where(where))}
