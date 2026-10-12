"""Fake-provider tests exercise actual DB and workflow; they do not call the paid API."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import User, Novel, Chapter, Revision
from app.services import save_revision, payload_hash
from app.generation.api import apply_job
from app.generation.context import safe_entry
from app.generation.models import GenerationJob, UsageLedger
from app.generation.pipeline import Pipeline, LeaseLost, validate_report
from app.generation.provider import ProviderResult
from app.generation.schemas import ApplyJob, Finding, PlanResult, ReviewResult, TextResult
from app.generation.validators import validate_text
from app.generation.worker import claim_job


def settings(**changes):
    values = dict(ai_max_calls=20, ai_max_output_tokens=4500, ai_timeout_seconds=5,
                  ai_input_usd_per_million=0.4, ai_output_usd_per_million=1.6,
                  ai_daily_budget_usd=5)
    return SimpleNamespace(**(values | changes))


def snapshot(**changes):
    values = dict(bible=[], rejected=[], chapter_number=1, chapter_id="chapter",
                  removed_fragments=[], settings={}, context_complete=True,
                  writer_context={"canon": []}, base_text="",
                  prompts={name: {"text": "test rules", "version": 0} for name in
                           ("architect", "writer", "continuity", "literary_editor", "revision", "commercial_editor")})
    return values | changes


class FakeProvider:
    def __init__(self, draft="Елена подошла к окну и вспомнила вчерашний разговор.",
                 revised=None, fail_on=None, invalid_review=False):
        self.draft, self.revised, self.fail_on = draft, revised or draft, fail_on
        self.invalid_review = invalid_review
        self.calls = []

    async def call(self, name, rules, payload, output_type, max_output_tokens):
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError("injected provider failure")
        if name == "architect":
            output = PlanResult(scenes=["Разговор у окна"], chapter_change="Новая цель",
                                unresolved_questions=[])
        elif name in ("writer", "revision"):
            output = TextResult(text=self.draft if name == "writer" else self.revised)
        else:
            findings = []
            if self.invalid_review:
                findings = [Finding(id="invalid", category="canon", severity="critical",
                                    quote="Нет такой цитаты", explanation="test",
                                    suggestion="test", rule_id="bible:missing")]
            output = ReviewResult(findings=findings, complete=True, limitations=[])
        return ProviderResult(output=output, input_tokens=50, output_tokens=50, requests=1)


@pytest.fixture
def storage(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "novels.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(email="author@example.test", password_hash="unused")
        db.add(user)
        db.flush()
        novel = Novel(owner_id=user.id, title="Наследство", genre="романтика", concept="Условие завещания")
        db.add(novel)
        db.flush()
        chapter = Chapter(novel_id=novel.id, number=1, title="Предложение")
        db.add(chapter)
        db.flush()
        initial = Revision(id=str(uuid4()), chapter_id=chapter.id, text="", source="initial",
                           mutation_id=str(uuid4()), payload_hash=payload_hash(None, ""))
        db.add(initial)
        db.flush()
        chapter.current_revision_id = initial.id
        db.commit()
        yield db, factory, user, novel, chapter
    engine.dispose()


def job_for(db, user, novel, chapter, **snapshot_changes):
    db.refresh(chapter)
    db.refresh(novel)
    data = snapshot(owner_id=user.id, chapter_id=chapter.id, **snapshot_changes)
    job = GenerationJob(id=str(uuid4()), novel_id=novel.id, chapter_id=chapter.id,
                        active_chapter_id=chapter.id, base_revision_id=chapter.current_revision_id,
                        canon_version=novel.canon_version, kind="write", instructions="Разговор",
                        idempotency_key=str(uuid4()), request_hash="hash", snapshot=data,
                        usage={"budget_usd": 1, "cost_usd": 0, "calls": []}, artifacts={})
    db.add(job)
    db.commit()
    return job


@pytest.mark.asyncio
async def test_independent_agents_and_candidate_only(storage):
    db, factory, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter)
    provider = FakeProvider()
    await Pipeline(db, job, provider, settings()).execute()
    assert job.status == "awaiting_author"
    assert provider.calls == ["architect", "writer", "continuity", "literary_editor"]
    db.refresh(chapter)
    assert chapter.current_revision_id == job.base_revision_id
    assert job.artifacts["revised_text"] == provider.draft
    assert all(c["input_tokens"] == 50 for c in job.usage["calls"])
    with factory() as reopened:
        persisted = reopened.get(GenerationJob, job.id)
        assert persisted.artifacts["draft_text"] == provider.draft


@pytest.mark.asyncio
async def test_three_failed_passes_stop_and_report_critical(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter,
                  rejected=[{"id": "ban", "phrase": "Запретная фраза", "status": "ACTIVE",
                             "severity": "critical", "scope": None}])
    provider = FakeProvider(draft="Запретная фраза появилась снова.")
    await Pipeline(db, job, provider, settings()).execute()
    assert job.status == "needs_author_review"
    assert job.review_passes == 3
    assert provider.calls.count("continuity") == 3
    assert provider.calls.count("literary_editor") == 3
    assert provider.calls.count("revision") == 2
    assert any(f["severity"] == "critical" for f in job.artifacts["findings"])


@pytest.mark.asyncio
async def test_revision_failure_preserves_original_draft(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter,
                  rejected=[{"id": "ban", "phrase": "Запретная фраза", "severity": "critical"}])
    provider = FakeProvider(draft="Запретная фраза.", fail_on="revision")
    await Pipeline(db, job, provider, settings()).execute()
    assert job.status == "failed"
    assert job.artifacts["draft_text"] == "Запретная фраза."
    assert chapter.current_revision_id == job.base_revision_id


@pytest.mark.asyncio
async def test_invalid_agent_citations_are_never_success(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter)
    await Pipeline(db, job, FakeProvider(invalid_review=True), settings()).execute()
    assert job.status == "needs_author_review"
    assert not job.artifacts["check_complete"]
    assert job.review_passes == 3
    assert job.artifacts["limitations"]


@pytest.mark.asyncio
async def test_restart_reuses_completed_checkpoints(storage):
    db, factory, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter)
    job.artifacts = {"checkpoints": {
        "architect": PlanResult(scenes=["a"], chapter_change="b", unresolved_questions=[]).model_dump(),
        "writer": TextResult(text="Сохранённый черновик после перезапуска.").model_dump()
    }}
    db.commit()
    job_id = job.id
    with factory() as restarted:
        restored = restarted.get(GenerationJob, job_id)
        provider = FakeProvider()
        await Pipeline(restarted, restored, provider, settings()).execute()
        assert "architect" not in provider.calls
        assert "writer" not in provider.calls
        assert restored.artifacts["draft_text"] == "Сохранённый черновик после перезапуска."


@pytest.mark.asyncio
async def test_manual_edit_is_not_overwritten_when_candidate_applied(storage):
    db, _, user, novel, chapter = storage
    initial = save_revision(db, chapter, chapter.current_revision_id, "Исходный текст.", "manual", str(uuid4()))
    job = job_for(db, user, novel, chapter)
    await Pipeline(db, job, FakeProvider(), settings()).execute()
    manual = save_revision(db, chapter, initial.id, "Последняя ручная правка.", "manual", str(uuid4()))
    with pytest.raises(HTTPException) as error:
        apply_job(job.id, ApplyJob(base_revision_id=initial.id, mutation_id=str(uuid4())),
                  db=db, user=user)
    assert error.value.status_code == 409
    db.refresh(chapter)
    assert chapter.current_revision_id == manual.id


@pytest.mark.asyncio
async def test_changed_canon_requires_new_check(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter)
    await Pipeline(db, job, FakeProvider(), settings()).execute()
    novel.canon_version += 1
    db.commit()
    with pytest.raises(HTTPException) as error:
        apply_job(job.id, ApplyJob(base_revision_id=job.base_revision_id, mutation_id=str(uuid4())), db=db, user=user)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_daily_budget_blocks_before_paid_call(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter)
    provider = FakeProvider()
    await Pipeline(db, job, provider, settings(ai_daily_budget_usd=0.00000001)).execute()
    assert provider.calls == []
    assert job.status == "failed"
    assert job.usage["cost_usd"] == 0


def test_queue_claim_and_expired_lease(storage):
    db, factory, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter)
    assert claim_job(factory, "worker-a", now=100) == job.id
    assert claim_job(factory, "worker-b", now=110) is None
    assert claim_job(factory, "worker-b", now=161) == job.id


def test_stale_worker_cannot_commit_candidate(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter)
    job.lease_owner = "new-worker"
    job.lease_until = 99999999999
    db.commit()
    with pytest.raises(LeaseLost):
        Pipeline(db, job, FakeProvider(), settings(), owner="old-worker").save(artifacts={"draft_text": "stale"})
    db.refresh(job)
    assert "draft_text" not in job.artifacts


def test_old_name_future_secret_and_proposals():
    data = snapshot(bible=[
        {"id": "name", "status": "CANON", "details": {"old_names": ["Анна"]}},
        {"id": "secret", "status": "FUTURE_CANON", "available_from_chapter": 10,
         "details": {"reveal_markers": ["завещание подделано"]}},
        {"id": "proposal", "status": "PROPOSAL", "details": {"old_names": ["Елена"]}}
    ])
    found = validate_text("Анна выяснила, что завещание подделано. Елена вошла.", data)
    assert {f.rule_id for f in found} == {"bible:name", "bible:secret"}
    assert all(f.severity == "critical" for f in found)


def test_future_unknown_release_is_hidden_and_detected():
    item = {"id": "secret", "kind": "future_vault", "status": "FUTURE_CANON", "active": True,
            "available_from_chapter": None, "details": {"reveal_markers": ["секретный отец"]}}
    assert not safe_entry(item, 1)
    assert validate_text("Он её секретный отец.", snapshot(bible=[item]))


def test_known_information_marker_requests_contextual_check():
    data = snapshot(bible=[{"id": "knowledge", "status": "FACT", "details": {
        "known_from_chapter": 5, "character_name": "Елена", "mention_markers": ["ключ в сейфе"]}}])
    found = validate_text("Елена поняла, что ключ в сейфе.", data)
    assert found[0].category == "continuity.knowledge"
    assert found[0].severity == "warning"


def test_time_precision_and_valid_exceptions():
    assert any(f.rule_id == "time.precision" for f in validate_text("Она проснулась в 7:15 и открыла окно.", snapshot()))
    assert any(f.rule_id == "time.precision" for f in validate_text("Спустя тридцать семь минут она ушла.", snapshot()))
    assert not validate_text("Встреча назначена на 14:30.", snapshot())
    assert not validate_text("Работу нужно закончить через два часа.", snapshot())
    assert not validate_text("Она проснулась в 7:15.", snapshot(settings={"exact_times": ["7:15"]}))


def test_repeated_phrase_and_author_removed_fragment():
    phrase = "Елена медленно провела ладонью по краю стола"
    found = validate_text((phrase + ". ") * 3, snapshot())
    assert any(f.rule_id == "repetition.phrase" for f in found)
    found = validate_text("Она всегда мечтала о таком муже.", snapshot(removed_fragments=["всегда мечтала о таком муже"]))
    assert any(f.rule_id == "manual.removal" and f.severity == "critical" for f in found)


def test_repeated_ban_is_critical_and_novels_do_not_mix():
    data = snapshot(rejected=[{"id": "ban", "phrase": "уголок губ дрогнул", "severity": "warning", "repeat_count": 2}])
    found = validate_text("Уголок губ дрогнул.", data)
    assert found[0].severity == "critical"
    assert not validate_text("Уголок губ дрогнул.", snapshot())


def test_report_invalid_quotes_and_unknown_rule_ids():
    report = ReviewResult(complete=True, limitations=[], findings=[Finding(
        id="a", category="continuity", severity="critical", quote="Вымышленная цитата",
        explanation="error", suggestion="fix", rule_id="bible:not-in-snapshot")])
    checked = validate_report(report, "Реальный текст.", snapshot(), "continuity")
    assert not checked.complete
    assert checked.findings == []


@pytest.mark.asyncio
async def test_real_sdk_adapter_configuration_and_usage(monkeypatch):
    from agents import Agent, Runner
    from agents.usage import Usage
    from app.generation.provider import SDKProvider
    configured = SimpleNamespace(openai_api_key="unit-test-key-not-real", openai_model="gpt-4.1-mini", ai_timeout_seconds=5)
    async def mocked_run(agent, payload, *, max_turns, run_config):
        assert isinstance(agent, Agent)
        assert max_turns == 1
        assert agent.output_type is TextResult
        assert agent.model_settings.max_tokens == 4500
        assert agent.model_settings.store is False
        assert run_config.tracing_disabled
        return SimpleNamespace(final_output=TextResult(text="Тестовый текст."),
                               context_wrapper=SimpleNamespace(usage=Usage(requests=1, input_tokens=123, output_tokens=456)))
    monkeypatch.setattr(Runner, "run", mocked_run)
    provider = SDKProvider(configured)
    try:
        assert provider.client.max_retries == 0
        result = await provider.call("writer", "rules", "input", TextResult, 4500)
        assert result.output.text == "Тестовый текст."
        assert (result.input_tokens, result.output_tokens, result.requests) == (123, 456, 1)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_correction_rechecks_and_preserves_original(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter, rejected=[{"id": "ban", "phrase": "Запретная фраза", "severity": "critical"}])
    provider = FakeProvider(draft="Запретная фраза.", revised="Елена выбрала другой ответ.")
    await Pipeline(db, job, provider, settings()).execute()
    assert job.status == "awaiting_author"
    assert job.review_passes == 2
    assert job.artifacts["draft_text"] == "Запретная фраза."
    assert job.artifacts["revised_text"] == "Елена выбрала другой ответ."
    assert job.artifacts["findings"] == []
    assert provider.calls.count("continuity") == 2
    assert provider.calls.count("literary_editor") == 2
    assert chapter.current_revision_id == job.base_revision_id


@pytest.mark.asyncio
async def test_truncated_guardian_context_is_not_marked_complete(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter, bible=[
        {"id": "long-fact", "kind": "core", "title": "Мир", "content": "a" * 50000,
         "status": "CANON", "active": True, "details": {}}
    ])
    await Pipeline(db, job, FakeProvider(), settings()).execute()
    assert job.status == "needs_author_review"
    assert not job.artifacts["check_complete"]
    assert job.artifacts["limitations"]


@pytest.mark.asyncio
async def test_apply_candidate_creates_revision_and_records_author_decision(storage):
    db, _, user, novel, chapter = storage
    job = job_for(db, user, novel, chapter)
    provider = FakeProvider()
    await Pipeline(db, job, provider, settings()).execute()
    result = apply_job(job.id, ApplyJob(base_revision_id=job.base_revision_id, mutation_id=str(uuid4())), db=db, user=user)
    db.refresh(chapter)
    assert result["revision"]["id"] == chapter.current_revision_id
    assert result["revision"]["text"] == provider.draft
    assert result["job"]["status"] == "applied"
    assert job.artifacts["author_decision"] == "accepted"


@pytest.mark.parametrize("status", ["IDEA", "PROPOSAL", "REJECTED", "UNDEFINED"])
def test_unapproved_bible_is_not_writer_context(status):
    assert not safe_entry({"active": True, "kind": "character", "status": status, "details": {}}, 1)


def test_uuid_ban_scope_and_whole_word_boundaries():
    chapter_id = str(uuid4())
    ban = {"id": "name", "phrase": "Анна", "scope": chapter_id, "status": "ACTIVE"}
    assert not validate_text("Анна вошла.", snapshot(chapter_id=str(uuid4()), rejected=[ban]))
    assert not validate_text("Аннаконда лежала.", snapshot(chapter_id=chapter_id, rejected=[ban]))
    assert validate_text("Анна вошла.", snapshot(chapter_id=chapter_id, rejected=[ban]))


@pytest.mark.parametrize("data", [
    {"kind": "future_vault", "status": "CANON", "details": {"reveal_markers": ["скрытый отец"]}},
    {"kind": "core", "status": "FACT", "available_from_chapter": 5, "details": {"reveal_markers": ["скрытый отец"]}},
])
def test_future_vault_and_delayed_facts_are_protected(data):
    assert any(f.rule_id == "bible:secret" for f in validate_text(
        "Он её скрытый отец.", snapshot(bible=[{"id": "secret", **data}])))


def test_future_fact_released_at_approved_chapter():
    item = {"id": "secret", "status": "FUTURE_CANON", "available_from_chapter": 5,
            "details": {"reveal_markers": ["скрытый отец"]}}
    assert not validate_text("Он её скрытый отец.", snapshot(chapter_number=5, bible=[item]))
