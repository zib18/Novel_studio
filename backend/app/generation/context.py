"""Bounded retrieval. Future vault is never passed to planning or prose agents."""
import json
import re
from pathlib import Path
from sqlalchemy import select
from app.models import BibleEntry, Chapter, Revision, RejectRule
from .models import PromptVersion

AGENTS = ("architect", "writer", "continuity", "literary_editor", "revision", "commercial_editor")
PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


def prompt_snapshot(db, novel_id: str) -> dict:
    result = {}
    for agent in AGENTS:
        row = db.scalar(select(PromptVersion).where(PromptVersion.novel_id == novel_id,
                        PromptVersion.agent == agent).order_by(PromptVersion.version.desc()).limit(1))
        result[agent] = {"version": row.version if row else 0,
                         "text": row.text if row else (PROMPTS / f"{agent}_rules.md").read_text(encoding="utf-8")}
    return result


def safe_entry(entry: dict, chapter_number: int) -> bool:
    status = entry["status"].upper().replace(" ", "_")
    kind = entry["kind"].lower()
    available = entry.get("available_from_chapter")
    if not entry.get("active") or status not in ("CANON", "FACT", "FUTURE_CANON"):
        return False
    if status == "FUTURE_CANON" and (available is None or available > chapter_number):
        return False
    if kind == "future_vault" and (available is None or available > chapter_number):
        return False
    if available is not None and available > chapter_number:
        return False
    try:
        raw_known = entry.get("details", {}).get("known_from_chapter")
        known_from = int(raw_known) if raw_known is not None else None
    except (TypeError, ValueError, OverflowError):
        return False
    if kind in ("knowledge", "knowledge_map") and (known_from is None or known_from > chapter_number):
        return False
    return True


def make_snapshot(db, novel, chapter, instructions: str, context_limit: int) -> dict:
    entries = [{"id": e.id, "kind": e.kind, "title": e.title, "content": e.content,
                "status": e.status, "active": e.active,
                "available_from_chapter": e.available_from_chapter, "details": e.details or {}}
               for e in db.scalars(select(BibleEntry).where(BibleEntry.novel_id == novel.id,
                                                            BibleEntry.active.is_(True)))]
    rejected = [{"id": r.id, "phrase": r.phrase, "description": r.description,
                 "scope": r.scope, "severity": r.severity, "repeat_count": r.repeat_count,
                 "status": r.status}
                for r in db.scalars(select(RejectRule).where(RejectRule.novel_id == novel.id))]
    terms = set(re.findall(r"\w{4,}", instructions.casefold()))
    safe = [e for e in entries if safe_entry(e, chapter.number)]
    safe.sort(key=lambda e: (
        e["kind"] in ("current_state", "novel_core", "character", "knowledge"),
        len(terms.intersection(re.findall(r"\w{4,}", (e["title"] + " " + e["content"]).casefold())))
    ), reverse=True)
    chosen, chars = [], 0
    for e in safe:
        item = {"id": "bible:" + e["id"], "kind": e["kind"], "title": e["title"],
                "content": e["content"], "status": e["status"]}
        size = len(json.dumps(item, ensure_ascii=False))
        if chars + size <= context_limit * 0.6:
            chosen.append(item)
            chars += size
    recent = []
    preceding = db.scalars(select(Chapter).where(Chapter.novel_id == novel.id,
                           Chapter.number < chapter.number,
                           Chapter.approved_revision_id.is_not(None))
                           .order_by(Chapter.number.desc()).limit(3)).all()
    for c in reversed(preceding):
        rev = db.get(Revision, c.approved_revision_id)
        if rev:
            allowance = max(0, min(context_limit // 4, context_limit - chars))
            recent.append({"number": c.number, "text": rev.text[-allowance:] if allowance else "",
                           "truncated": len(rev.text) > allowance})
            chars += min(len(rev.text), allowance)
    current = db.get(Revision, chapter.current_revision_id) if chapter.current_revision_id else None
    removed = []
    for r in db.scalars(select(Revision).where(Revision.chapter_id == chapter.id)
                        .order_by(Revision.created_at.desc()).limit(30)):
        removed.extend(x for x in (r.deleted_fragments or []) if isinstance(x, str))
    if current:
        removed = [fragment for fragment in removed if fragment.casefold() not in current.text.casefold()]
    rules = [r for r in rejected if r["status"].lower() not in ("resolved", "inactive")
             and (r["scope"] is None or r["scope"] == chapter.id)]
    safe_context = {"novel": {"title": novel.title, "genre": novel.genre, "concept": novel.concept},
                    "chapter_number": chapter.number, "canon": chosen, "recent_chapters": recent,
                    "author_instructions": instructions, "prohibitions": rules,
                    "removed_fragments": removed[:100],
                    "settings": {k: v for k, v in (novel.settings or {}).items()
                                 if k in ("target_min_chars", "target_max_chars", "pace", "literary_instructions", "exact_times", "pinned_scenes")}}
    return {"bible": entries, "rejected": rejected, "chapter_number": chapter.number,
            "chapter_id": chapter.id, "removed_fragments": removed[:100],
            "settings": novel.settings or {}, "writer_context": safe_context,
            "base_text": current.text if current else "",
            "prompts": prompt_snapshot(db, novel.id),
            "context_complete": len(chosen) == len(safe) and not any(r["truncated"] for r in recent)}


def guardian_context(snapshot: dict, text: str, limit: int) -> tuple[dict, bool]:
    terms = set(re.findall(r"\w{4,}", text.casefold()))
    def relevance(entry):
        details = entry.get("details") or {}
        markers = []
        for key in ("reveal_markers", "mention_markers"):
            value = details.get(key, [])
            markers.extend(value if isinstance(value, list) else [value] if isinstance(value, str) else [])
        direct = any(isinstance(m, str) and m.casefold() in text.casefold() for m in markers)
        words = re.findall(r"\w{4,}", (entry["title"] + " " + entry["content"]).casefold())
        return (direct, entry["kind"].lower() in ("current_state", "knowledge"),
                len(terms.intersection(words)))
    entries = sorted(snapshot["bible"], key=relevance, reverse=True)
    chosen, size = [], len(text) + 2048
    for entry in entries:
        entry_size = len(json.dumps(entry, ensure_ascii=False))
        if size + entry_size <= limit:
            chosen.append(entry)
            size += entry_size
    bans = []
    for rule in sorted(snapshot["rejected"],
                       key=lambda r: bool(r.get("phrase")) and r["phrase"].casefold() in text.casefold(),
                       reverse=True):
        rule_size = len(json.dumps(rule, ensure_ascii=False))
        if size + rule_size <= limit:
            bans.append(rule)
            size += rule_size
    return {"text": text, "chapter_number": snapshot["chapter_number"],
            "bible": chosen, "rejected": bans}, (
                len(chosen) == len(entries) and len(bans) == len(snapshot["rejected"]))
