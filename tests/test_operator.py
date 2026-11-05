import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import Settings
from app.main import create_app
from app.models import HelpdeskEvent, Job
from app.operator import csrf_token

pytestmark = pytest.mark.asyncio(loop_scope="module")
AUTH = ("operator", "test-password")


@pytest_asyncio.fixture(loop_scope="module")
async def client(database_url: str, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        operator_username=AUTH[0],
        operator_password=AUTH[1],
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", auth=AUTH
        ) as client:
            yield client


async def seed(engine: AsyncEngine, *, ready: bool = True) -> UUID:
    async with async_sessionmaker(engine, expire_on_commit=False).begin() as session:
        session.add(
            HelpdeskEvent(
                event_id="review",
                ticket_id="T1",
                customer_email="user@example.com",
                message="<script>alert(1)</script>",
                reply_url="http://mock/reply",
            )
        )
        await session.flush()
        job = Job(
            event_id="review",
            kind="generation",
            state="succeeded" if ready else "pending",
            result={
                "message": "Approved draft <b>text</b>",
                "sources": [],
                "needs_clarification": False,
            }
            if ready
            else None,
        )
        session.add(job)
        await session.flush()
        return job.id


async def post(client: AsyncClient, job_id: UUID, action: str):
    settings = Settings(_env_file=None, operator_password=AUTH[1])
    return await client.post(
        f"/operator/jobs/{job_id}/{action}", data={"csrf": csrf_token(settings, job_id, action)}
    )


async def delivery_count(engine: AsyncEngine) -> int:
    async with engine.connect() as c:
        return await c.scalar(select(func.count()).select_from(Job).where(Job.kind == "delivery"))


async def test_requires_auth_on_page_and_actions(client: AsyncClient, engine: AsyncEngine) -> None:
    job = await seed(engine)
    assert (await client.get("/operator/", auth=None)).status_code == 401
    assert (await client.get("/operator/", auth=("operator", "wrong"))).status_code == 401
    assert (await client.post(f"/operator/jobs/{job}/approve", auth=None)).status_code == 401
    assert await delivery_count(engine) == 0


async def test_page_escapes_text_and_has_review_forms(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    job = await seed(engine)
    response = await client.get("/operator/")
    assert response.status_code == 200
    assert "<script>" not in response.text and "&lt;script&gt;" in response.text
    assert "&lt;b&gt;text&lt;/b&gt;" in response.text
    assert f"/operator/jobs/{job}/approve" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert await delivery_count(engine) == 0


async def test_csrf_is_required_and_bound_to_action(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    job = await seed(engine)
    assert (
        await client.post(f"/operator/jobs/{job}/approve", data={"csrf": "bad"})
    ).status_code == 403
    token = csrf_token(Settings(_env_file=None, operator_password=AUTH[1]), job, "reject")
    assert (
        await client.post(f"/operator/jobs/{job}/approve", data={"csrf": token})
    ).status_code == 403
    assert await delivery_count(engine) == 0


async def test_concurrent_approval_queues_one_frozen_message(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    job = await seed(engine)
    responses = await asyncio.gather(*(post(client, job, "approve") for _ in range(10)))
    assert all(r.status_code == 303 for r in responses)
    assert await delivery_count(engine) == 1
    async with async_sessionmaker(engine)() as session:
        event = await session.get(HelpdeskEvent, "review")
        delivery = (await session.scalars(select(Job).where(Job.kind == "delivery"))).one()
        assert event.decision == "approved" and event.reviewed_at is not None
        assert delivery.payload == {"message": "Approved draft <b>text</b>"}
        assert delivery.state == "pending"
    assert (await post(client, job, "reject")).status_code == 409


async def test_rejection_is_idempotent_and_cannot_be_approved(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    job = await seed(engine)
    assert (await post(client, job, "reject")).status_code == 303
    assert (await post(client, job, "reject")).status_code == 303
    assert (await post(client, job, "approve")).status_code == 409
    assert await delivery_count(engine) == 0


async def test_competing_approve_reject_has_one_winner(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    job = await seed(engine)
    responses = await asyncio.gather(post(client, job, "approve"), post(client, job, "reject"))
    assert sorted(r.status_code for r in responses) == [303, 409]
    async with async_sessionmaker(engine)() as session:
        event = await session.get(HelpdeskEvent, "review")
        expected = 1 if event.decision == "approved" else 0
    assert await delivery_count(engine) == expected


async def test_approval_rolls_back_if_delivery_insert_fails(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    job = await seed(engine)
    async with engine.begin() as c:
        await c.execute(
            text("ALTER TABLE jobs ADD CONSTRAINT block_delivery CHECK (kind <> 'delivery')")
        )
    try:
        # Simulates DB failure after the decision was changed in memory.
        assert (await post(client, job, "approve")).status_code == 503
        async with async_sessionmaker(engine)() as session:
            assert (await session.get(HelpdeskEvent, "review")).decision == "pending"
        assert await delivery_count(engine) == 0
    finally:
        async with engine.begin() as c:
            await c.execute(text("ALTER TABLE jobs DROP CONSTRAINT block_delivery"))
    assert (await post(client, job, "approve")).status_code == 303


async def test_pending_draft_and_unknown_id_cannot_be_approved(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    job = await seed(engine, ready=False)
    assert (await post(client, job, "approve")).status_code == 409
    assert (await post(client, uuid4(), "approve")).status_code == 404
    assert await delivery_count(engine) == 0
