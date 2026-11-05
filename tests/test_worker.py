import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models import HelpdeskEvent, Job, LLMAdmission
from app.queue import Claim, Queue
from app.worker import PermanentError, RetryableError, Worker

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize(
    "error,code",
    [
        (PermanentError, "permanent_handler_failure"),
        (ValueError, "unexpected_handler_failure"),
    ],
)
async def test_non_retryable_failure(
    engine: AsyncEngine,
    error: type[Exception],
    code: str,
) -> None:
    queue = await seed(engine)

    async def broken(claim: Claim) -> dict:
        raise error("private provider response")

    assert await Worker(queue, {"generation": broken}).run_once()
    async with queue.sessions() as session:
        job = (await session.scalars(select(Job))).one()
        assert job.state == "failed" and job.last_error == code and job.attempts == 1
    assert not await queue.claim(("generation",))


async def test_claim_skips_a_locked_job(engine: AsyncEngine) -> None:
    queue = await seed(engine, 2)
    async with engine.begin() as connection:
        locked = await connection.scalar(select(Job.id).order_by(Job.id).limit(1).with_for_update())
        claim = await asyncio.wait_for(queue.claim(("generation",)), timeout=2)
        assert claim is not None and claim.id != locked


async def seed(engine: AsyncEngine, count: int = 1) -> Queue:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        for i in range(count):
            session.add(
                HelpdeskEvent(
                    event_id=f"evt{i}",
                    ticket_id="T1",
                    customer_email="test@example.com",
                    message="test",
                    reply_url="http://mock/reply",
                )
            )
        await session.flush()
        session.add_all(Job(event_id=f"evt{i}", kind="generation") for i in range(count))
    return Queue(sessions)


async def expire(engine: AsyncEngine, claim: Claim) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Job)
            .where(Job.id == claim.id)
            .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
        )


async def test_global_concurrency_and_unique_claims(engine: AsyncEngine) -> None:
    queue = await seed(engine, 12)
    other = Queue(async_sessionmaker(engine, expire_on_commit=False))
    claims = await asyncio.gather(
        *((queue if i % 2 else other).claim(("generation",)) for i in range(12))
    )
    owned = [c for c in claims if c is not None]
    assert len(owned) == len({c.id for c in owned}) == 4
    assert await queue.finish(owned[0], {"draft": "test"})
    assert await other.claim(("generation",)) is not None


async def test_rate_limit_is_atomic_and_expires(engine: AsyncEngine) -> None:
    queue = await seed(engine, 4)
    async with queue.sessions.begin() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        session.add_all(LLMAdmission(admitted_at=now) for _ in range(59))
    claims = await asyncio.gather(*(queue.claim(("generation",)) for _ in range(4)))
    assert sum(c is not None for c in claims) == 1
    async with engine.begin() as connection:
        assert await connection.scalar(select(func.count()).select_from(LLMAdmission)) == 60
        await connection.execute(
            update(LLMAdmission).values(admitted_at=func.clock_timestamp() - timedelta(seconds=61))
        )
    assert await queue.claim(("generation",)) is not None


async def test_recovery_fences_old_success_and_failure(engine: AsyncEngine) -> None:
    queue = await seed(engine)
    first = await queue.claim(("generation",))
    assert first is not None
    await expire(engine, first)
    assert not await queue.finish(first, {"stale": True})
    second = await Queue(queue.sessions).claim(("generation",))
    assert second is not None and second.id == first.id and second.token != first.token
    assert second.attempt == 2
    assert not await queue.fail(first, "old_failure", retryable=True)
    assert await queue.finish(second, {"fresh": True})
    assert not await queue.finish(first, {"stale": True})
    async with queue.sessions() as session:
        assert (await session.get(Job, first.id)).result == {"fresh": True}


async def test_crashes_exhaust_attempts(engine: AsyncEngine) -> None:
    queue = await seed(engine)
    for attempt in range(1, 4):
        claim = await queue.claim(("generation",))
        assert claim is not None and claim.attempt == attempt
        await expire(engine, claim)
    assert await queue.claim(("generation",)) is None
    async with queue.sessions() as session:
        job = await session.get(Job, claim.id)
        assert job.state == "failed" and job.last_error == "lease_expired"


async def test_retry_backoff_and_rate_accounting(engine: AsyncEngine) -> None:
    queue = await seed(engine)

    async def failing(claim: Claim) -> dict:
        raise RetryableError("secret provider text")

    worker = Worker(queue, {"generation": failing})
    for attempt in range(1, 4):
        assert await worker.run_once()
        async with queue.sessions() as session:
            job = (await session.scalars(select(Job))).one()
            now = await session.scalar(select(func.clock_timestamp()))
            assert job.attempts == attempt
            assert job.available_at > now
            assert job.last_error == "temporary_handler_failure"
            assert job.state == ("pending" if attempt < 3 else "failed")
            assert await session.scalar(select(func.count()).select_from(LLMAdmission)) == attempt
        assert not await worker.run_once()
        async with engine.begin() as connection:
            await connection.execute(update(Job).values(available_at=func.clock_timestamp()))


async def test_handler_runs_without_job_lock(engine: AsyncEngine) -> None:
    queue = await seed(engine)

    async def handle(claim: Claim) -> dict:
        async with engine.begin() as connection:
            await connection.execute(
                select(Job).where(Job.id == claim.id).with_for_update(nowait=True)
            )
        return {"draft": "result"}

    assert await Worker(queue, {"generation": handle}).run_once()
    async with queue.sessions() as session:
        job = (await session.scalars(select(Job))).one()
        assert job.state == "succeeded" and job.result == {"draft": "result"}


async def test_cancelled_worker_can_be_recovered(engine: AsyncEngine) -> None:
    queue = await seed(engine)
    started = asyncio.Event()

    async def blocked(claim: Claim) -> dict:
        started.set()
        await asyncio.Event().wait()
        return {}

    task = asyncio.create_task(Worker(queue, {"generation": blocked}).run_once())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with engine.begin() as connection:
        await connection.execute(
            update(Job).values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
        )
    recovered = await Queue(queue.sessions).claim(("generation",))
    assert recovered is not None and recovered.attempt == 2


async def test_timeout_retries_and_unknown_jobs_stay_pending(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = await seed(engine)
    assert not await Worker(queue, {}).run_once()
    monkeypatch.setattr("app.worker.HANDLER_TIMEOUT", 0.02)

    async def slow(claim: Claim) -> dict:
        await asyncio.sleep(1)
        return {}

    assert await Worker(queue, {"generation": slow}).run_once()
    async with queue.sessions() as session:
        job = (await session.scalars(select(Job))).one()
        assert job.state == "pending" and job.attempts == 1


@pytest.mark.parametrize(
    "error",
    [OperationalError(None, None, Exception("PRIVATE_DATABASE_DETAIL")), OSError("offline")],
)
async def test_handler_storage_outage_retries_then_recovers(
    engine: AsyncEngine, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    queue = await seed(engine)
    calls = 0

    async def temporarily_unavailable(claim: Claim) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return {"message": "Recovered"}

    worker = Worker(queue, {"generation": temporarily_unavailable})
    assert await worker.run_once()
    async with queue.sessions.begin() as session:
        job = (await session.scalars(select(Job))).one()
        assert job.state == "pending" and job.last_error == "temporary_handler_failure"
        job.available_at = func.clock_timestamp()
    assert await worker.run_once()
    async with queue.sessions() as session:
        job = (await session.scalars(select(Job))).one()
        assert job.state == "succeeded" and job.attempts == 2
        assert job.result == {"message": "Recovered"}
        assert await session.scalar(select(func.count()).select_from(LLMAdmission)) == 2
    assert "PRIVATE_DATABASE_DETAIL" not in caplog.text


async def test_storage_outage_does_not_stop_worker_loop(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    queue = await seed(engine)
    original_claim = queue.claim
    attempts = 0
    completed = asyncio.Event()

    async def flaky_claim(kinds: tuple[str, ...]) -> Claim | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(None, None, Exception("PRIVATE_DATABASE_DETAIL"))
        return await original_claim(kinds)

    async def handle(claim: Claim) -> dict:
        completed.set()
        return {"ok": True}

    monkeypatch.setattr(queue, "claim", flaky_claim)
    task = asyncio.create_task(Worker(queue, {"generation": handle}).run())
    try:
        await asyncio.wait_for(completed.wait(), timeout=5)
        assert attempts >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "Worker storage unavailable" in caplog.text
    assert "PRIVATE_DATABASE_DETAIL" not in caplog.text


async def test_delivery_is_not_blocked_by_exhausted_llm_budget(engine: AsyncEngine) -> None:
    queue = await seed(engine)
    async with queue.sessions.begin() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        session.add_all(LLMAdmission(admitted_at=now) for _ in range(60))
        session.add(Job(event_id="evt0", kind="delivery", payload={"message": "Approved"}))
    claim = await queue.claim(("generation", "delivery"))
    assert claim is not None and claim.kind == "delivery"
    assert await queue.finish(claim, {"status": "sent"})
    assert await queue.claim(("generation", "delivery")) is None
    async with queue.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(LLMAdmission)) == 60
