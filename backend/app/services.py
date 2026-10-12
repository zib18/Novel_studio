import difflib
import hashlib
import json
import re
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
    """Linear trimming; detailed matching only for a middle up to 20k chars.

    Large replacements conservatively retain the entire removed middle as a
    constraint. Full immutable revisions are never truncated by this function.
    """
    if before == after:
        return []
    prefix = 0
    common = min(len(before), len(after))
    while prefix < common and before[prefix] == after[prefix]:
        prefix += 1
    end_before, end_after = len(before), len(after)
    while (end_before > prefix and end_after > prefix
           and before[end_before - 1] == after[end_after - 1]):
        end_before -= 1
        end_after -= 1
    old_middle = before[prefix:end_before]
    new_middle = after[prefix:end_after]
    if not old_middle:
        return []

    def meaningful(fragment):
        words = 0
        for _ in re.finditer(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", fragment):
            words += 1
            if words >= 3:
                return True
        return words >= 2 and len(fragment.strip()) >= 12

    if len(old_middle) + len(new_middle) > 20000:
        return [old_middle] if meaningful(old_middle) else []
    fragments = []
    matcher = difflib.SequenceMatcher(None, old_middle, new_middle, autojunk=True)
    for tag, start, stop, _, _ in matcher.get_opcodes():
        fragment = old_middle[start:stop]
        if tag in ("delete", "replace") and meaningful(fragment):
            fragments.append(fragment)
    return list(dict.fromkeys(fragments))

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
    canon_changed = db.execute(update(Novel).where(
        Novel.id == novel.id, Novel.canon_version == decision.expected_canon_version
    ).values(canon_version=Novel.canon_version + 1, updated_at=utcnow())
                               .execution_options(synchronize_session=False)).rowcount
    changed = db.execute(update(BibleEntry).where(
        BibleEntry.id == entry.id, BibleEntry.version == decision.base_version,
        BibleEntry.active.is_(True)
    ).values(status=target, version=entry.version + 1)
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
        old_name = old.details.get("name")
        new_name = entry.details.get("name")
        old_name = old_name if isinstance(old_name, str) and old_name.strip() else old.title
        new_name = new_name if isinstance(new_name, str) and new_name.strip() else entry.title
        if old_name.strip().casefold() != new_name.strip().casefold():
            generated = db.scalars(select(RejectRule).where(
                RejectRule.novel_id == novel.id, RejectRule.status == "ACTIVE",
                RejectRule.source_decision_id.in_(select(AuthorDecision.id).where(
                    AuthorDecision.novel_id == novel.id, AuthorDecision.action == "approve"))
            ))
            for prior in generated:
                if prior.phrase.strip().casefold() == new_name.strip().casefold():
                    prior.status = "RESOLVED"
            db.add(RejectRule(novel_id=novel.id, phrase=old_name,
                              description=f"Автор заменил имя «{old_name}» на «{new_name}»",
                              source_decision_id=audit.id, severity="critical"))
    if target == "REJECTED":
        db.add(RejectRule(novel_id=novel.id, phrase=entry.details.get("rejected_phrase", "")
                          if isinstance(entry.details.get("rejected_phrase", ""), str) else "",
                          description=f"Отклонено автором: {entry.title}. {decision.reason}",
                          source_decision_id=audit.id, severity="critical"))
    db.commit()
    db.refresh(entry)
    db.refresh(novel)
    return {"entry": record_dict(entry), "decision_id": audit.id, "canon_version": novel.canon_version}
