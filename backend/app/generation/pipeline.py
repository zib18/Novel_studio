"""Checkpointed workflow. Candidate prose never updates a Chapter."""
import asyncio
import hashlib
import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from openai import APIConnectionError, APIStatusError, RateLimitError
from .models import GenerationJob, UsageLedger, utc_now
from .schemas import Finding, PlanResult, TextResult, ReviewResult
from .validators import validate_text
from .context import guardian_context
from app.models import RejectRule

EDITOR_RULES = {"prose.flow", "dialogue.natural", "dialogue.voice", "gesture.variety",
                "emotion.depth", "behavior.logic", "romance.gradual", "chapter.pacing",
                "chapter.ending", "time.precision", "repetition.phrase", "commercial.retention"}


class CancelledJob(Exception):
    pass


class LeaseLost(Exception):
    pass


class BudgetExceeded(Exception):
    pass


def validate_report(report: ReviewResult, text: str, snapshot: dict, role: str) -> ReviewResult:
    ids = {"bible:" + e["id"] for e in snapshot["bible"]
           if e.get("active") is not False and
           str(e.get("status", "")).upper().replace(" ", "_") in ("CANON", "FACT", "FUTURE_CANON")}
    ids |= {"reject:" + r["id"] for r in snapshot["rejected"]
            if r.get("active") is not False and str(r.get("status", "ACTIVE")).upper()
            not in ("RESOLVED", "INACTIVE", "DISABLED", "REVOKED", "DELETED")}
    ids |= EDITOR_RULES | {"manual.removal"}
    valid, invalid = [], []
    for finding in report.findings:
        if not finding.quote or finding.quote not in text or finding.rule_id not in ids:
            invalid.append(finding.id)
        else:
            item = finding.model_copy(update={"status": "open"})
            valid.append(item)
    return ReviewResult(findings=valid, complete=report.complete and not invalid,
                        limitations=report.limitations +
                        ([f"{role}: недействительные цитаты/ссылки: " + ", ".join(invalid)] if invalid else []))


def serialize(result):
    return result.model_dump()


class Pipeline:
    def __init__(self, db, job: GenerationJob, provider, settings, owner: str | None = None):
        self.db, self.job, self.provider, self.settings, self.owner = db, job, provider, settings, owner

    def check(self):
        self.db.refresh(self.job)
        if self.owner and (self.job.lease_owner != self.owner or (self.job.lease_until or 0) < time.time()):
            raise LeaseLost()
        if self.job.cancel_requested:
            raise CancelledJob()

    def save(self, *, artifacts=None, usage=None, stage=None, **fields):
        self.check()
        values = {"updated_at": utc_now(), **fields}
        if artifacts is not None:
            values["artifacts"] = deepcopy(artifacts)
        if usage is not None:
            values["usage"] = deepcopy(usage)
        if stage:
            values["stage"] = stage
        condition = GenerationJob.id == self.job.id
        if self.owner:
            condition = condition & (GenerationJob.lease_owner == self.owner) & (
                GenerationJob.lease_until > time.time()) & GenerationJob.cancel_requested.is_(False)
        changed = self.db.execute(update(GenerationJob).where(condition).values(**values)
                                  .execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            self.db.rollback()
            self.check()
            raise LeaseLost()
        self.db.commit()
        self.db.refresh(self.job)

    async def call(self, checkpoint: str, agent: str, payload: dict, schema):
        self.check()
        artifacts = deepcopy(self.job.artifacts)
        completed = artifacts.setdefault("checkpoints", {})
        if checkpoint in completed:
            return schema.model_validate(completed[checkpoint])
        prompt = self.job.snapshot["prompts"][agent]["text"]
        content = json.dumps(payload, ensure_ascii=False)
        output_limit = self.settings.ai_max_output_tokens
        schema_size = len(json.dumps(schema.model_json_schema()))
        input_upper = len((prompt + content).encode("utf-8")) + schema_size + 2048
        reserved = (input_upper * self.settings.ai_input_usd_per_million +
                    output_limit * self.settings.ai_output_usd_per_million) / 1_000_000
        for retry in range(2):
            self.check()
            usage = deepcopy(self.job.usage)
            calls = usage.setdefault("calls", [])
            prior = sum(c["checkpoint"] == checkpoint for c in calls)
            if prior >= 2:
                raise RuntimeError("Лимит повторов этапа исчерпан; предыдущий расход может быть неопределён.")
            if len(calls) >= self.settings.ai_max_calls:
                raise BudgetExceeded("Достигнут лимит запросов задачи.")
            if usage.get("cost_usd", 0) + reserved > usage["budget_usd"]:
                raise BudgetExceeded("Оставшегося бюджета недостаточно для следующего запроса.")
            day = datetime.now(timezone.utc).date().isoformat()
            user_id = self.job.snapshot["owner_id"]
            ledger = self.db.scalar(select(UsageLedger).where(UsageLedger.user_id == user_id,
                                                             UsageLedger.day == day))
            if ledger is None:
                try:
                    with self.db.begin_nested():
                        self.db.add(UsageLedger(user_id=user_id, day=day, cost_usd=0.0))
                        self.db.flush()
                except IntegrityError:
                    pass
            changed = self.db.execute(update(UsageLedger).where(
                UsageLedger.user_id == user_id, UsageLedger.day == day,
                UsageLedger.cost_usd + reserved <= self.settings.ai_daily_budget_usd
            ).values(cost_usd=UsageLedger.cost_usd + reserved)).rowcount
            if changed != 1:
                raise BudgetExceeded("Достигнут дневной лимит расходов автора (UTC).")
            call_record = {"checkpoint": checkpoint, "agent": agent, "cost_usd": reserved,
                           "input_tokens": 0, "output_tokens": 0, "requests": 1,
                           "uncertain": True, "reservation": True, "ledger_day": day}
            calls.append(call_record)
            usage["cost_usd"] = usage.get("cost_usd", 0) + reserved
            self.save(usage=usage, stage=checkpoint)
            try:
                result = await asyncio.wait_for(
                    self.provider.call(agent, prompt, content, schema, output_limit),
                    timeout=self.settings.ai_timeout_seconds)
            except Exception as exc:
                self.check()
                usage = deepcopy(self.job.usage)
                usage["calls"][-1]["reservation"] = False
                usage["calls"][-1]["error_type"] = type(exc).__name__
                self.save(usage=usage)
                transient = isinstance(exc, (APIConnectionError, RateLimitError))
                transient |= isinstance(exc, APIStatusError) and exc.status_code >= 500
                if transient and retry == 0:
                    await asyncio.sleep(1)
                    continue
                raise
            self.check()
            usage = deepcopy(self.job.usage)
            record = usage["calls"][-1]
            actual = (result.input_tokens * self.settings.ai_input_usd_per_million +
                      result.output_tokens * self.settings.ai_output_usd_per_million) / 1_000_000
            uncertain = result.requests < 1 or result.input_tokens <= 0
            charged = reserved if uncertain else actual
            usage["cost_usd"] = usage["cost_usd"] - reserved + charged
            self.db.execute(update(UsageLedger).where(UsageLedger.user_id == self.job.snapshot["owner_id"],
                UsageLedger.day == record["ledger_day"]).values(
                    cost_usd=UsageLedger.cost_usd + charged - reserved))
            record.update(cost_usd=charged, input_tokens=result.input_tokens,
                          output_tokens=result.output_tokens, requests=max(1, result.requests),
                          uncertain=uncertain, reservation=False)
            artifacts = deepcopy(self.job.artifacts)
            artifacts.setdefault("checkpoints", {})[checkpoint] = serialize(result.output)
            self.save(usage=usage, artifacts=artifacts)
            return result.output
        raise RuntimeError("Этап не завершён.")

    def finish_cancel(self):
        self.db.rollback()
        condition = GenerationJob.id == self.job.id
        if self.owner:
            condition = condition & (GenerationJob.lease_owner == self.owner)
        self.db.execute(update(GenerationJob).where(condition).values(
            status="cancelled", stage="cancelled", active_chapter_id=None,
            lease_owner=None, lease_until=None, updated_at=utc_now()
        ).execution_options(synchronize_session=False))
        self.db.commit()
        self.db.refresh(self.job)

    async def execute(self):
        try:
            await self._execute()
        except LeaseLost:
            return
        except CancelledJob:
            self.finish_cancel()
        except Exception as exc:
            try:
                self.save(status="failed", stage="failed", active_chapter_id=None,
                          error=(str(exc) if isinstance(exc, BudgetExceeded) else
                                 "Ошибка этапа: " + type(exc).__name__ + ". Сохранённые результаты доступны."),
                          lease_owner=None, lease_until=None)
            except CancelledJob:
                self.finish_cancel()
            except LeaseLost:
                return

    async def _execute(self):
        self.check()
        snapshot = self.job.snapshot
        context = snapshot["writer_context"]
        if self.job.kind == "write":
            plan = await self.call("architect", "architect", context, PlanResult)
            text_result = await self.call("writer", "writer",
                                          {"context": context, "plan": serialize(plan)}, TextResult)
            draft = text_result.text
        else:
            plan = None
            draft = snapshot["base_text"]
        if not draft.strip():
            raise ValueError("Нет текста для проверки.")
        artifacts = deepcopy(self.job.artifacts)
        artifacts["plan"] = serialize(plan) if plan else None
        artifacts["draft_text"] = draft
        if self.job.kind == "write":
            artifacts["writer_proposals"] = text_result.proposals
        self.save(artifacts=artifacts)
        text = artifacts.get("revised_text", draft)
        for index in range(self.job.max_review_passes):
            pass_no = index + 1
            pass_texts = deepcopy(self.job.artifacts).setdefault("pass_texts", {})
            if str(pass_no) in pass_texts:
                text = pass_texts[str(pass_no)]
            else:
                pass_texts[str(pass_no)] = text
                artifacts = deepcopy(self.job.artifacts)
                artifacts["pass_texts"] = pass_texts
                self.save(artifacts=artifacts)
            mechanical = validate_text(text, snapshot)
            guardian_input, guardian_complete = guardian_context(
                snapshot, text, getattr(self.settings, "ai_context_chars", 40000))
            guardian_input["author_instructions"] = self.job.instructions
            continuity = await self.call(f"continuity:{pass_no}", "continuity",
                                         guardian_input, ReviewResult)
            literary = await self.call(f"literary:{pass_no}", "literary_editor",
                         {"text": text, "context": context, "allowed_rule_ids": sorted(EDITOR_RULES)},
                         ReviewResult)
            continuity = validate_report(continuity, text, snapshot, "continuity")
            literary = validate_report(literary, text, snapshot, "literary_editor")
            findings = mechanical + continuity.findings + literary.findings
            for finding in findings:
                finding.id = hashlib.sha256((finding.rule_id + "\0" + finding.quote).encode()).hexdigest()[:20]
                if pass_no > 1 and finding.rule_id.startswith("reject:"):
                    finding.severity = "critical"
            strongest = {}
            severity_rank = {"warning": 1, "error": 2, "critical": 3}
            for finding in findings:
                key = (finding.rule_id, finding.quote)
                existing = strongest.get(key)
                if existing is None or severity_rank[finding.severity] > severity_rank[existing.severity]:
                    strongest[key] = finding
            findings = list(strongest.values())
            complete = continuity.complete and literary.complete and guardian_complete
            limitations = continuity.limitations + literary.limitations
            if not snapshot["context_complete"] or not guardian_complete:
                limitations.append("Контекст ограничен: включены только релевантные факты и последние главы; "
                                   "литеральные запреты проверены по всему снимку.")
            blocking = any(f.severity in ("critical", "error") or f.rule_id.startswith("reject:") for f in findings)
            artifacts = deepcopy(self.job.artifacts)
            artifacts["revised_text"] = text
            registered = set(artifacts.get("registered_rejects", []))
            for finding in mechanical:
                if finding.rule_id.startswith("reject:"):
                    rule_id = finding.rule_id.split(":", 1)[1]
                    if rule_id not in registered:
                        self.db.execute(update(RejectRule).where(
                            RejectRule.id == rule_id, RejectRule.novel_id == self.job.novel_id
                        ).values(repeat_count=RejectRule.repeat_count + 1))
                        registered.add(rule_id)
            artifacts["registered_rejects"] = sorted(registered)
            artifacts["findings"] = [serialize(f) for f in findings]
            artifacts["check_complete"] = complete
            artifacts["limitations"] = limitations
            history = artifacts.setdefault("history", [])
            current_ids = {finding.id for finding in findings}
            for previous_pass in history:
                for previous in previous_pass.get("findings", []):
                    if previous.get("status") != "rejected":
                        previous["status"] = "open" if previous["id"] in current_ids else "fixed"
            record = {"pass": pass_no, "text": text, "findings": artifacts["findings"],
                      "check_complete": complete, "limitations": limitations}
            if len(history) >= pass_no:
                history[pass_no - 1] = record
            else:
                history.append(record)
            self.save(artifacts=artifacts, review_passes=pass_no)
            if not blocking and complete:
                if snapshot.get("commercial_report"):
                    report = await self.call("commercial", "commercial_editor",
                                            {"text": text, "context": context,
                                             "allowed_rule_ids": sorted(EDITOR_RULES)}, ReviewResult)
                    artifacts = deepcopy(self.job.artifacts)
                    artifacts["commercial_report"] = serialize(validate_report(report, text, snapshot, "commercial"))
                    self.save(artifacts=artifacts)
                self.save(status="awaiting_author", stage="complete", active_chapter_id=None,
                          lease_owner=None, lease_until=None)
                return
            if pass_no >= self.job.max_review_passes:
                self.save(status="needs_author_review", stage="review_limit", active_chapter_id=None,
                          lease_owner=None, lease_until=None)
                return
            if blocking:
                revision = await self.call(f"revision:{pass_no}", "revision",
                                {"text": text, "context": context,
                                 "findings": [serialize(f) for f in findings]}, TextResult)
                text = revision.text
                artifacts = deepcopy(self.job.artifacts)
                artifacts["revised_text"] = text
                artifacts.setdefault("revision_proposals", []).append(revision.proposals)
                self.save(artifacts=artifacts)
