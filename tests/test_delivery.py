import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import Settings
from app.delivery import DeliveryHandler, HTTPHelpdeskAdapter
from app.errors import PermanentError, RetryableError
from app.mock_helpdesk import create_mock_app
from app.models import HelpdeskEvent, Job, MockReceipt
from app.queue import Queue
from app.reply_urls import reply_url_allowed
from app.worker import Worker

pytestmark = pytest.mark.asyncio(loop_scope="module")
HEADERS = {"Idempotency-Key": "test-key", "Authorization": "Bearer local-mock-token"}
BODY = {"ticket_id": "T1", "message": "Approved answer"}


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/reply",
        "http://mock-helpdesk.evil:8000/reply",
        "http://user@mock-helpdesk:8000/reply",
        "http://mock-helpdesk:8001/reply",
        "http://mock-helpdesk:8000/reply#fragment",
        "file:///secret",
        "http://127.0.0.1:8000/reply",
    ],
)
async def test_url_policy_rejects_untrusted_destinations(url: str) -> None:
    assert not reply_url_allowed(url, ["http://mock-helpdesk:8000"])


async def test_rejected_destination_makes_no_http_request() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Untrusted destination contacted")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(PermanentError):
            await HTTPHelpdeskAdapter(Settings(_env_file=None), client).send(
                "http://127.0.0.1/private", "T1", "answer", "key"
            )


@pytest.mark.parametrize(
    "status,error",
    [
        (200, PermanentError),
        (302, PermanentError),
        (400, PermanentError),
        (429, RetryableError),
        (503, RetryableError),
    ],
)
async def test_delivery_requires_201_and_never_follows_redirects(status, error) -> None:
    calls = []

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"Location": "http://evil.example"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(serve), follow_redirects=True
    ) as client:
        with pytest.raises(error):
            await HTTPHelpdeskAdapter(Settings(_env_file=None), client).send(
                "http://mock-helpdesk:8000/reply", "T1", "answer", "stable-key"
            )
    assert len(calls) == 1
    assert calls[0].headers["Idempotency-Key"] == "stable-key"


async def test_mock_concurrent_dedup_conflict_and_restart(
    engine: AsyncEngine, database_url: str
) -> None:
    config = Settings(_env_file=None, database_url=database_url)
    app = create_mock_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://mock"
        ) as client:
            responses = await asyncio.gather(
                *(client.post("/tickets/T1/replies", json=BODY, headers=HEADERS) for _ in range(10))
            )
            assert all(r.status_code == 201 for r in responses)
            conflict = await client.post(
                "/tickets/T1/replies", json={**BODY, "message": "changed"}, headers=HEADERS
            )
            assert conflict.status_code == 409
    restarted = create_mock_app(config)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(restarted), base_url="http://mock"
        ) as client:
            assert (
                await client.post("/tickets/T1/replies", json=BODY, headers=HEADERS)
            ).status_code == 201
            assert (await client.get("/messages/T1")).status_code == 401
    async with engine.connect() as c:
        assert await c.scalar(select(func.count()).select_from(MockReceipt)) == 1


async def test_lost_ack_retries_same_key_without_duplicate(
    engine: AsyncEngine, database_url: str
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as s:
        s.add(
            HelpdeskEvent(
                event_id="delivery",
                ticket_id="T1",
                customer_email="user@example.com",
                message="question",
                reply_url="http://mock-helpdesk:8000/tickets/T1/replies",
                decision="approved",
            )
        )
        await s.flush()
        s.add(Job(event_id="delivery", kind="delivery", payload={"message": BODY["message"]}))
    app = create_mock_app(Settings(_env_file=None, database_url=database_url))
    keys = []
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app)

        class LoseFirstAck(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                keys.append(request.headers["Idempotency-Key"])
                response = await transport.handle_async_request(request)
                if len(keys) == 1:
                    await response.aclose()
                    raise httpx.ReadTimeout("Acknowledgment lost", request=request)
                return response

        async with httpx.AsyncClient(transport=LoseFirstAck()) as client:
            handler = DeliveryHandler(
                sessions, HTTPHelpdeskAdapter(Settings(_env_file=None), client)
            )
            worker = Worker(Queue(sessions), {"delivery": handler})
            assert await worker.run_once()
            async with sessions.begin() as s:
                assert await s.scalar(select(func.count()).select_from(MockReceipt)) == 1
                job = (await s.scalars(select(Job))).one()
                assert job.state == "pending"
                job.available_at = func.clock_timestamp()
            assert await Worker(Queue(sessions), {"delivery": handler}).run_once()
    assert len(keys) == 2 and keys[0] == keys[1]
    async with sessions() as s:
        assert (await s.scalars(select(Job))).one().state == "succeeded"
        assert await s.scalar(select(func.count()).select_from(MockReceipt)) == 1


@pytest.mark.parametrize("decision", ["pending", "rejected"])
async def test_unapproved_event_never_calls_sender(engine: AsyncEngine, decision: str) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as s:
        s.add(
            HelpdeskEvent(
                event_id="blocked",
                ticket_id="T1",
                customer_email="user@example.com",
                message="question",
                reply_url="http://mock-helpdesk:8000/reply",
                decision=decision,
            )
        )
        await s.flush()
        s.add(Job(event_id="blocked", kind="delivery", payload={"message": "answer"}))

    class ForbiddenSender:
        async def send(self, *args) -> None:
            pytest.fail("Unapproved delivery attempted")

    worker = Worker(Queue(sessions), {"delivery": DeliveryHandler(sessions, ForbiddenSender())})
    assert await worker.run_once()
    async with sessions() as s:
        assert (await s.scalars(select(Job))).one().state == "failed"


async def test_mock_failure_modes(
    engine: AsyncEngine, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_mock_app(Settings(_env_file=None, database_url=database_url))
    delays = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    # Replace only mock module's sleep reference, preserving asyncio used by drivers.
    monkeypatch.setattr("app.mock_helpdesk.asyncio", SimpleNamespace(sleep=sleep))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://mock"
        ) as c:
            for scenario, first in [
                ("fail-once", 503),
                ("always-500", 503),
                ("timeout-before", 503),
                ("accept-timeout", 201),
            ]:
                headers = {**HEADERS, "Idempotency-Key": str(uuid4())}
                url = f"/tickets/T1/replies?scenario={scenario}"
                assert (await c.post(url, json=BODY, headers=headers)).status_code == first
                assert (await c.post(url, json=BODY, headers=headers)).status_code == (
                    503 if scenario == "always-500" else 201
                )
    assert delays == [8, 8]
