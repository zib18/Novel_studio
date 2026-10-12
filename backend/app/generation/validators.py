"""Deterministic checks. Semantic continuity still requires an independent agent."""
from __future__ import annotations
import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any
from uuid import UUID
from .schemas import Finding

_CANON_STATUSES = {"CANON", "FACT", "FUTURE CANON"}
_DISABLED_STATUSES = {"INACTIVE", "DISABLED", "REVOKED", "DELETED", "RESOLVED"}
_SEVERITIES = {"critical", "error", "warning"}
_AMOUNT_WORD = (
    r"(?:один|одну|одного|одна|два|две|двух|три|трёх|трех|"
    r"четыре|четырёх|четырех|пять|пяти|шесть|шести|семь|семи|"
    r"восемь|восьми|девять|девяти|десять|десяти|"
    r"одиннадцать|двенадцать|тринадцать|четырнадцать|"
    r"пятнадцать|шестнадцать|семнадцать|восемнадцать|"
    r"девятнадцать|двадцать|тридцать|сорок|пятьдесят|"
    r"шестьдесят|семьдесят|восемьдесят|девяносто|сто)"
)
_AMOUNT = rf"(?:\d+|{_AMOUNT_WORD}(?:\s+{_AMOUNT_WORD}){{0,2}})"
_TIME_PATTERNS = (
    re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b"),
    re.compile(
        rf"\b(?:спустя|через|ровно|в\s+течение)\s+{_AMOUNT}\s+"
        r"(?:минут(?:а|ы|у)?|час(?:а|ов)?|секунд(?:а|ы|у)?)\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:в|к|на|ровно)\s+{_AMOUNT}\s+"
        r"(?:час(?:а|ов)?|утра|дня|вечера|ночи)\b", re.IGNORECASE),
    re.compile(rf"\b(?:в|к|ровно)\s+{_AMOUNT_WORD}\s+{_AMOUNT_WORD}\b", re.IGNORECASE),
)
_TIME_CONTEXT = re.compile(
    r"встреч|назначен|договор|дедлайн|срок|опозд|"
    r"расписан|поезд|электричк|автобус|рейс|вылет|"
    r"алиби|улика|расследован|следств|запис[ьи]|камер|"
    r"законч|сдать|обещал|буду\s+дома", re.IGNORECASE)
_WORDS = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", re.UNICODE)

def _status(value: Any) -> str:
    return str(value or "").strip().upper().replace("_", " ").replace("-", " ")

def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]

def _details(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("details")
    return value if isinstance(value, Mapping) else {}

def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []

def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default

def _matches(text: str, phrase: str) -> list[re.Match[str]]:
    """Case-insensitive literal phrase, flexible spaces, whole-word edges."""
    phrase = phrase.strip()
    if not phrase:
        return []
    pattern = r"\s+".join(re.escape(part) for part in phrase.split())
    if phrase[0].isalnum() or phrase[0] == "_":
        pattern = r"(?<!\w)" + pattern
    if phrase[-1].isalnum() or phrase[-1] == "_":
        pattern += r"(?!\w)"
    return list(re.finditer(pattern, text, flags=re.IGNORECASE))

def _sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(char, 0, start) for char in ".!?\n") + 1
    next_end = re.search(r"[.!?\n]", text[end:])
    right = end + next_end.end() if next_end else len(text)
    return text[left:right].strip()

def _finding(*, rule_id: str, category: str, severity: str, quote: str,
             explanation: str, suggestion: str, position: int) -> Finding:
    digest = hashlib.sha256(f"{rule_id}|{position}|{quote}".encode("utf-8")).hexdigest()[:20]
    return Finding(id=f"finding_{digest}", category=category, severity=severity,
                   quote=quote, explanation=explanation, suggestion=suggestion,
                   rule_id=rule_id, status="open")

def _scope_applies(scope: Any, chapter_number: int, chapter_id: Any) -> bool:
    if isinstance(scope, Mapping):
        scoped_id = scope.get("chapter_id")
        if scoped_id is not None and str(scoped_id) != str(chapter_id):
            return False
        chapter = scope.get("chapter_number")
        return chapter is None or _integer(chapter, -1) == chapter_number
    value = str(scope or "").strip()
    try:
        scoped_uuid = UUID(value)
    except (ValueError, AttributeError, TypeError):
        pass
    else:
        try:
            return scoped_uuid == UUID(str(chapter_id))
        except (ValueError, AttributeError, TypeError):
            return False
    match = re.fullmatch(r"(?:chapter|глава)\s*:\s*(\d+)", value.lower())
    return match is None or int(match.group(1)) == chapter_number

def _author_time_setting(snapshot: Mapping[str, Any]) -> Any:
    settings = snapshot.get("settings")
    if isinstance(settings, Mapping) and "exact_times" in settings:
        return settings["exact_times"]
    novel = snapshot.get("novel")
    if isinstance(novel, Mapping):
        settings = novel.get("settings")
        if isinstance(settings, Mapping):
            return settings.get("exact_times", [])
    return snapshot.get("exact_times", [])

def _time_is_allowed(text: str, match: re.Match[str], setting: Any) -> bool:
    if setting is True:
        return True
    allowed = _strings(setting)
    if isinstance(setting, (list, tuple)):
        allowed += [str(item["value"]) for item in setting
                    if isinstance(item, Mapping) and item.get("value")]
    for phrase in allowed:
        for permitted in _matches(text, phrase):
            if permitted.start() <= match.start() and permitted.end() >= match.end():
                return True
    context = _sentence(text, match.start(), match.end())
    if _TIME_CONTEXT.search(context):
        return True
    line_start = text.rfind("\n", 0, match.start()) + 1
    if text[line_start:match.start()].lstrip().startswith(("—", "–", "-")):
        return True
    before = text[:match.start()]
    if before.count("«") > before.count("»") and "»" in text[match.end():]:
        return True
    return False

def validate_text(text: str, snapshot: Mapping[str, Any]) -> list[Finding]:
    """Literal checks of bans, future/knowledge markers, deleted text, time, repetition.
    Findings need context review; they do not establish semantic correctness.
    """
    findings: list[Finding] = []
    chapter_number = _integer(snapshot.get("chapter_number"), 1)
    chapter_id = snapshot.get("chapter_id")
    for item in _rows(snapshot.get("rejected")):
        if not item.get("id") or _status(item.get("status")) in _DISABLED_STATUSES:
            continue
        if item.get("active") is False or not _scope_applies(item.get("scope"), chapter_number, chapter_id):
            continue
        phrase = str(item.get("phrase") or "").strip()
        severity = str(item.get("severity") or "error").lower()
        if severity not in _SEVERITIES:
            severity = "error"
        repeat_count = _integer(item.get("repeat_count"))
        if repeat_count > 0:
            severity = "critical"
        for match in _matches(text, phrase):
            findings.append(_finding(
                rule_id=f"reject:{item['id']}", category="author.ban", severity=severity,
                quote=match.group(),
                explanation=f"Обнаружено буквальное совпадение с запретом: {item.get('description') or phrase}."
                + (f" Уже зарегистрировано повторений: {repeat_count}; повторное нарушение критическое."
                   if repeat_count > 0 else ""),
                suggestion="Удалить или переписать фрагмент согласно решению автора.", position=match.start()))
    for item in _rows(snapshot.get("bible")):
        if not item.get("id") or item.get("active") is False or _status(item.get("status")) not in _CANON_STATUSES:
            continue
        status = _status(item.get("status"))
        details = _details(item)
        rule_id = f"bible:{item['id']}"
        for phrase in _strings(details.get("old_names")) + _strings(details.get("forbidden_phrases")):
            for match in _matches(text, phrase):
                findings.append(_finding(
                    rule_id=rule_id, category="canon.old_value", severity="critical", quote=match.group(),
                    explanation="Возвращено явно отменённое значение из утверждённой записи "
                                f"«{item.get('title') or item['id']}».",
                    suggestion="Использовать актуальное решение автора.", position=match.start()))
        raw_available = item.get("available_from_chapter")
        if raw_available is None:
            raw_available = details.get("available_from_chapter")
        available_from = _integer(raw_available, 0)
        is_future = status == "FUTURE CANON" or _status(item.get("kind")) in {"FUTURE VAULT", "FUTURE"}
        disclosure_blocked = (available_from > 0 and chapter_number < available_from) or (is_future and available_from <= 0)
        if disclosure_blocked:
            availability = (f"Раскрытие разрешено с главы {available_from}, текущая — {chapter_number}."
                            if available_from > 0 else "Глава раскрытия ещё не утверждена автором.")
            for phrase in _strings(details.get("reveal_markers")):
                for match in _matches(text, phrase):
                    findings.append(_finding(
                        rule_id=rule_id, category="continuity.future", severity="critical", quote=match.group(),
                        explanation=f"Совпадение с заданным маркером раскрытия события «{item.get('title') or item['id']}». {availability}",
                        suggestion="Убрать раскрытие; независимый хранитель канона должен проверить контекст и возможные косвенные раскрытия.",
                        position=match.start()))
        known_from = _integer(details.get("known_from_chapter"), 0)
        if known_from and chapter_number < known_from:
            character = str(details.get("character_name") or "").strip()
            for phrase in _strings(details.get("mention_markers")):
                for match in _matches(text, phrase):
                    context = _sentence(text, match.start(), match.end())
                    if character and not _matches(context, character):
                        continue
                    findings.append(_finding(
                        rule_id=rule_id, category="continuity.knowledge", severity="warning", quote=context,
                        explanation=f"Упомянут заданный маркер информации, доступной с главы {known_from}. Буквальная проверка не доказывает, что персонаж использует неизвестное ему знание.",
                        suggestion="Проверить, кто говорит или думает, откуда получены сведения и не является ли фрагмент предположением.",
                        position=match.start()))
    for phrase in _strings(snapshot.get("removed_fragments")):
        for match in _matches(text, phrase):
            findings.append(_finding(
                rule_id="manual.removal", category="manual.restore", severity="critical", quote=match.group(),
                explanation="Возвращён фрагмент, явно удалённый автором.",
                suggestion="Сохранить последнюю ручную правку и убрать возвращённый текст.", position=match.start()))
    allowed_times = _author_time_setting(snapshot)
    checked_time_spans: list[tuple[int, int]] = []
    for pattern in _TIME_PATTERNS:
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if any(span[0] < previous[1] and span[1] > previous[0] for previous in checked_time_spans):
                continue
            checked_time_spans.append(span)
            if _time_is_allowed(text, match, allowed_times):
                continue
            findings.append(_finding(
                rule_id="time.precision", category="style.time", severity="warning", quote=match.group(),
                explanation="Возможно излишне точное обозначение времени. Простая проверка не нашла в предложении признаков встречи, срока, расписания или другого предусмотренного исключения.",
                suggestion="Проверить сюжетную необходимость. Если точность не нужна, использовать естественное относительное обозначение времени.",
                position=match.start()))
    words = list(_WORDS.finditer(text))
    windows: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index in range(max(0, len(words) - 5)):
        signature = tuple(word.group().casefold() for word in words[index:index + 6])
        windows[signature].append(index)
    covered: set[int] = set()
    for positions in sorted(windows.values(), key=lambda value: value[0]):
        if len(positions) < 3 or positions[0] in covered:
            continue
        first = positions[0]
        quote = text[words[first].start():words[first + 5].end()]
        findings.append(_finding(
            rule_id="repetition.phrase", category="style.repetition", severity="warning", quote=quote,
            explanation=f"Последовательность из шести слов встречается {len(positions)} раз в этой главе. Совпадение может быть намеренным художественным приёмом.",
            suggestion="Проверить функцию повтора; при необходимости разнообразить формулировку.", position=words[first].start()))
        for position in positions:
            covered.update(range(position, position + 6))
    return list({finding.id: finding for finding in findings}.values())
