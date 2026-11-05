import asyncio
from collections.abc import AsyncIterator
from time import perf_counter

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.main import create_app
from app.models import HelpdeskEvent, Job

pytestmark = pytest.mark.asyncio(loop_scope="module")
PAYLOAD = {
    "event_id": "evt_test",
    "ticket_id": "T-1042",
    "customer_email": "user@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "http://mock-helpdesk:8000/tickets/T-1042/replies",
}


@pytest_asyncio.fixture(loop_scope="module")
async def client(database_url: str, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    application = create_app(Settings(_env_file=None, database_url=database_url))
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            yield client


async def counts(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as connection:
        events = await connection.scalar(select(func.count()).select_from(HelpdeskEvent))
        jobs = await connection.scalar(select(func.count()).select_from(Job))
    return events, jobs


async def test_commits_event_and_pending_job_before_ack(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    started = perf_counter()
    response = await client.post("/webhooks/helpdesk", json=PAYLOAD)
    assert perf_counter() - started < 2
    assert response.status_code == 202
    assert response.json() == {"event_id": "evt_test", "status": "accepted"}
    assert await counts(engine) == (1, 1)
    async with engine.connect() as connection:
        row = (await connection.execute(select(Job.state, Job.kind, Job.attempts))).one()
    assert tuple(row) == ("pending", "generation", 0)


async def test_concurrent_duplicates_across_two_api_instances(
    client: AsyncClient,
    engine: AsyncEngine,
    database_url: str,
) -> None:
    second_app = create_app(Settings(_env_file=None, database_url=database_url))
    async with second_app.router.lifespan_context(second_app):
        async with AsyncClient(
            transport=ASGITransport(app=second_app), base_url="http://second"
        ) as second_client:
            gate = asyncio.Event()

            async def deliver(index: int) -> Response:
                await gate.wait()
                sender = client if index % 2 else second_client
                return await sender.post("/webhooks/helpdesk", json=PAYLOAD)

            tasks = [asyncio.create_task(deliver(i)) for i in range(20)]
            gate.set()
            responses = await asyncio.gather(*tasks)
    assert all(r.status_code == 202 for r in responses)
    assert sum(r.json()["status"] == "accepted" for r in responses) == 1
    assert await counts(engine) == (1, 1)


async def test_conflicting_event_does_not_overwrite(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    await client.post("/webhooks/helpdesk", json=PAYLOAD)
    response = await client.post("/webhooks/helpdesk", json={**PAYLOAD, "message": "Other text"})
    assert response.status_code == 409
    assert await counts(engine) == (1, 1)
    async with engine.connect() as connection:
        assert await connection.scalar(select(HelpdeskEvent.message)) == PAYLOAD["message"]


async def test_distinct_events_are_not_deduplicated(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    responses = await asyncio.gather(
        *(
            client.post("/webhooks/helpdesk", json={**PAYLOAD, "event_id": f"evt_{i}"})
            for i in range(20)
        )
    )
    assert all(r.status_code == 202 and r.json()["status"] == "accepted" for r in responses)
    assert await counts(engine) == (20, 20)


async def test_locked_database_returns_retryable_error_under_two_seconds(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("LOCK TABLE helpdesk_events IN ACCESS EXCLUSIVE MODE"))
        started = perf_counter()
        response = await client.post("/webhooks/helpdesk", json=PAYLOAD)
        assert perf_counter() - started < 2
        assert response.status_code == 503
    assert await counts(engine) == (0, 0)
    assert (await client.post("/webhooks/helpdesk", json=PAYLOAD)).status_code == 202
    assert await counts(engine) == (1, 1)


async def test_failed_job_insert_rolls_back_event(
    client: AsyncClient,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Fault injection in PostgreSQL, after the event INSERT and before commit.
    async with engine.begin() as connection:
        await connection.execute(text("ALTER TABLE jobs ADD CONSTRAINT reject_jobs CHECK (false)"))
    try:
        response = await client.post("/webhooks/helpdesk", json=PAYLOAD)
        assert response.status_code == 503
        assert await counts(engine) == (0, 0)
        assert PAYLOAD["message"] not in caplog.text
        assert PAYLOAD["customer_email"] not in caplog.text
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE jobs DROP CONSTRAINT reject_jobs"))
    retry = await client.post("/webhooks/helpdesk", json=PAYLOAD)
    assert retry.status_code == 202
    assert await counts(engine) == (1, 1)


async def test_duplicate_survives_api_restart(
    client: AsyncClient,
    engine: AsyncEngine,
    database_url: str,
) -> None:
    await client.post("/webhooks/helpdesk", json=PAYLOAD)
    restarted = create_app(Settings(_env_file=None, database_url=database_url))
    async with restarted.router.lifespan_context(restarted):
        async with AsyncClient(
            transport=ASGITransport(app=restarted), base_url="http://new"
        ) as new:
            response = await new.post("/webhooks/helpdesk", json=PAYLOAD)
    assert response.json()["status"] == "duplicate"
    assert await counts(engine) == (1, 1)


async def test_database_outage_returns_retryable_error() -> None:
    application = create_app(
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unavailable",
        )
    )
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as c:
            started = perf_counter()
            response = await c.post("/webhooks/helpdesk", json=PAYLOAD)
            assert perf_counter() - started < 2
            assert response.status_code == 503
            assert response.headers["retry-after"] == "1"
            assert (await c.get("/health")).status_code == 200


@pytest.mark.parametrize(
    "patch",
    [
        {"event_id": " "},
        {"message": ""},
        {"customer_email": "invalid"},
        {"reply_url": "file:///secret"},
        {"reply_url": "http://user:pass@host/replies"},
        {"message": "x" * 20001},
    ],
)
async def test_invalid_payload_creates_no_records(
    client: AsyncClient,
    engine: AsyncEngine,
    patch: dict[str, str],
) -> None:
    assert (await client.post("/webhooks/helpdesk", json={**PAYLOAD, **patch})).status_code == 422
    assert await counts(engine) == (0, 0)
