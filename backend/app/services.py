import difflib
import hashlib
import json
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from .auth import fail
from .models import AuthorDecision, BibleEntry, Chapter, Novel, RejectRule, Revision, uid, utcnow

APPROVED = ("CANON", "FACT", "FUTURE_CANON")


def owned_novel(db, novel_id, user):
    novel = db.scalar(select(Novel).where(Novel.id == novel_id, Novel.owner_id == user.id))
    if novel is None:
        fail(404, "not_found", "Роман не найден")
    return novel


def owned_chapter(db, chapter_id, user):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        fail(404, "not_found", "Глава не найдена")
    owned_novel(db, chapter.novel_id, user)
    return chapter


def owned_entry(db, entry_id, user):
    entry = db.get(BibleEntry, entry_id)
    if entry is None:
        fail(404, "not_found", "Запись не найдена")
    owned_novel(db, entry.novel_id, user)
    return entry


def record_dict(record, omit=()):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns
            if column.name not in omit}


def revision_meta(revision):
    return record_dict(revision, ("text", "payload_hash", "deleted_fragments"))


def chapter_detail(db, chapter):
    db.refresh(chapter)
    result = record_dict(chapter)
    result["current_revision"] = record_dict(db.get(Revision, chapter.current_revision_id),
                                            ("payload_hash",)) if chapter.current_revision_id else None
    return result


def payload_hash(base_id, text):
    data = json.dumps([base_id, text], ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def deleted_fragments(before, after):
    fragments = []
    for tag, start, stop, _, _ in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        fragment = before[start:stop]
        if tag in ("delete", "replace") and len(fragment.strip()) >= 12:
            fragments.append(fragment)
    return fragments


def save_revision(db, chapter, base_revision_id, text, source, mutation_id, *, commit=True):
    digest = payload_hash(base_revision_id, text)
    previous = db.scalar(select(Revision).where(Revision.chapter_id == chapter.id,
                                               Revision.mutation_id == mutation_id))
    if previous:
        if previous.payload_hash != digest:
            fail(409, "mutation_reused", "Идентификатор изменения уже использован с другим текстом")
        return previous
    base = db.get(Revision, base_revision_id)
    if not base or base.chapter_id != chapter.id:
        fail(409, "revision_conflict", "Исходная редакция не принадлежит главе")
    revision = Revision(id=uid(), chapter_id=chapter.id, parent_revision_id=base_revision_id,
                        text=text, source=source, mutation_id=mutation_id, payload_hash=digest,
                        deleted_fragments=deleted_fragments(base.text, text)
                        if source in ("manual", "restore") else [])
    try:
        db.add(revision)
        db.flush()
        changed = db.execute(update(Chapter).where(
            Chapter.id == chapter.id, Chapter.current_revision_id == base_revision_id
        ).values(current_revision_id=revision.id, status="DRAFT", updated_at=utcnow())
                             .execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            db.rollback()
            latest = db.get(Chapter, chapter.id)
            fail(409, "revision_conflict", "Глава изменена на другом устройстве; ваша версия сохранена локально",
                 current_revision_id=latest.current_revision_id if latest else None)
        if commit:
            db.commit()
        return revision
    except IntegrityError:
        db.rollback()
        previous = db.scalar(select(Revision).where(Revision.chapter_id == chapter.id,
                                                   Revision.mutation_id == mutation_id))
        if previous and previous.payload_hash == digest:
            return previous
        fail(409, "revision_conflict", "Изменение конфликтует с уже сохранённой редакцией")


def confirm_entry(db, entry, novel, user, decision):
    if entry.version != decision.base_version or not entry.active:
        fail(409, "entry_conflict", "Запись уже изменена")
    if entry.status not in ("IDEA", "PROPOSAL", "UNDEFINED") and decision.action == "approve":
        fail(422, "invalid_decision", "Утверждать можно только новую идею или предложение")
    target = decision.target_status if decision.action == "approve" else "REJECTED"
    if target == "FUTURE_CANON" or (decision.action == "approve" and entry.kind == "future_vault"):
        if entry.available_from_chapter is None:
            fail(422, "missing_reveal_chapter", "Укажите главу, начиная с которой возможно раскрытие")
        target = "FUTURE_CANON"
    old = db.get(BibleEntry, entry.supersedes_id) if entry.supersedes_id else None
    if old and decision.action == "approve" and (not old.active or old.status not in APPROVED):
        fail(409, "replacement_conflict", "Заменяемый факт уже изменён")
    changed = db.execute(update(BibleEntry).where(
        BibleEntry.id == entry.id, BibleEntry.version == decision.base_version,
        BibleEntry.active.is_(True)
    ).values(status=target, version=entry.version + 1)
                         .execution_options(synchronize_session=False)).rowcount
    canon_changed = db.execute(update(Novel).where(
        Novel.id == novel.id, Novel.canon_version == decision.expected_canon_version
    ).values(canon_version=Novel.canon_version + 1, updated_at=utcnow())
                               .execution_options(synchronize_session=False)).rowcount
    if changed != 1 or canon_changed != 1:
        db.rollback()
        fail(409, "canon_conflict", "Канон изменился; обновите его перед подтверждением")
    if old and decision.action == "approve":
        retired = db.execute(update(BibleEntry).where(
            BibleEntry.id == old.id, BibleEntry.active.is_(True), BibleEntry.version == old.version
        ).values(active=False, version=old.version + 1)
                              .execution_options(synchronize_session=False)).rowcount
        if retired != 1:
            db.rollback()
            fail(409, "replacement_conflict", "Заменяемый факт уже изменён")
    audit = AuthorDecision(id=uid(), novel_id=novel.id, user_id=user.id, bible_entry_id=entry.id,
                           action=decision.action, reason=decision.reason,
                           payload={"target_status": target, "title": entry.title, "content": entry.content,
                                    "details": entry.details, "supersedes_id": entry.supersedes_id,
                                    "previous_content": old.content if old else None,
                                    "previous_details": old.details if old else None,
                                    "canon_version": decision.expected_canon_version + 1})
    db.add(audit)
    db.flush()
    if old and target in APPROVED and old.kind == "character":
        old_name = old.details.get("name") or old.title
        new_name = entry.details.get("name") or entry.title
        if old_name.strip().casefold() != new_name.strip().casefold():
            db.add(RejectRule(novel_id=novel.id, phrase=old_name,
                              description=f"Автор заменил имя «{old_name}» на «{new_name}»",
                              source_decision_id=audit.id, severity="critical"))
    if target == "REJECTED":
        db.add(RejectRule(novel_id=novel.id, phrase=entry.details.get("rejected_phrase", ""),
                          description=f"Отклонено автором: {entry.title}. {decision.reason}",
                          source_decision_id=audit.id, severity="critical"))
    db.commit()
    db.refresh(entry)
    db.refresh(novel)
    return {"entry": record_dict(entry), "decision_id": audit.id, "canon_version": novel.canon_version}
