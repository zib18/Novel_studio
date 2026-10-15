"""Run separately: python -m app.generation.worker. Web clients may disconnect."""
import asyncio
import logging
import time
from uuid import uuid4
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import sessionmaker
from app.config import Settings
from app.database import make_engine
from app import models as core_models
from .models import GenerationJob, PromptVersion, UsageLedger, utc_now
from .pipeline import Pipeline
from .provider import SDKProvider

log = logging.getLogger("novel-studio.worker")
LEASE_SECONDS = 60.0
HEARTBEAT_SECONDS = 10.0


def claim_job(factory, owner: str, now: float | None = None):
    now = now or time.time()
    live_novel = select(core_models.Novel.id).where(
        core_models.Novel.id == GenerationJob.novel_id,
        core_models.Novel.deleted_at.is_(None)).exists()
    eligible = and_(GenerationJob.status.in_(("queued", "running")),
                    GenerationJob.cancel_requested.is_(False), live_novel,
                    or_(GenerationJob.lease_until.is_(None), GenerationJob.lease_until < now))
    with factory() as db:
        candidates = db.scalars(select(GenerationJob.id).where(eligible)
                               .order_by(GenerationJob.created_at).limit(10)).all()
        for job_id in candidates:
            changed = db.execute(update(GenerationJob).where(GenerationJob.id == job_id, eligible)
                                 .values(status="running", lease_owner=owner,
                                         lease_until=now + LEASE_SECONDS,
                                         attempts=GenerationJob.attempts + 1, updated_at=utc_now())).rowcount
            db.commit()
            if changed == 1:
                return job_id
    return None


async def heartbeat(factory, job_id, owner, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), HEARTBEAT_SECONDS)
        except TimeoutError:
            with factory() as db:
                changed = db.execute(update(GenerationJob).where(
                    GenerationJob.id == job_id, GenerationJob.lease_owner == owner,
                    GenerationJob.status == "running").values(
                        lease_until=time.time() + LEASE_SECONDS)).rowcount
                db.commit()
                if changed != 1:
                    return


async def run_once(factory, provider, settings, owner):
    job_id = claim_job(factory, owner)
    if job_id is None:
        return False
    stop = asyncio.Event()
    beat = asyncio.create_task(heartbeat(factory, job_id, owner, stop))
    try:
        with factory() as db:
            job = db.get(GenerationJob, job_id)
            await Pipeline(db, job, provider, settings, owner=owner).execute()
    finally:
        stop.set()
        await beat
    return True


async def main():
    settings = Settings()
    key = settings.openai_api_key
    if hasattr(key, "get_secret_value"):
        key = key.get_secret_value()
    if not key:
        raise SystemExit("NOVEL_OPENAI_API_KEY не настроен; платные запросы не выполнялись.")
    engine = make_engine(settings.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner = str(uuid4())
    provider = SDKProvider(settings)
    try:
        while True:
            if not await run_once(factory, provider, settings, owner):
                await asyncio.sleep(2)
    finally:
        await provider.close()
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Worker остановлен; незавершённые задания возобновятся после окончания lease.")
