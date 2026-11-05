import asyncio
import json
import re

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import Settings
from app.delivery import DeliveryHandler, HTTPHelpdeskAdapter
from app.generation import GenerationHandler
from app.knowledge import ARTICLES, Article
from app.llm import Candidate
from app.main import create_app
from app.mock_helpdesk import create_mock_app
from app.models import HelpdeskEvent, Job
from app.queue import Queue
from app.worker import Worker, main

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_complete_workflow_from_webhook_through_operator_to_helpdesk(
    engine: AsyncEngine, database_url: str, action: str
) -> None:
    settings = Settings(_env_file=None, database_url=database_url)
    app, mock = create_app(settings), create_mock_app(settings)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    searches = []

    def external_search(message: str) -> list[Article]:
        searches.append(message)
        return [ARTICLES[0]]

    class OtherProvider:
        async def generate(self, message: str, articles: list[Article]) -> Candidate:
            return Candidate(message="Експорт → PDF.", article_ids=[articles[0].id])

    async with app.router.lifespan_context(app), mock.router.lifespan_context(mock):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://app") as api,
            httpx.AsyncClient(transport=httpx.ASGITransport(mock)) as helpdesk,
        ):
            worker = Worker(
                Queue(sessions),
                {
                    "generation": GenerationHandler(
                        sessions, OtherProvider(), "http://app", search=external_search
                    ),
                    "delivery": DeliveryHandler(sessions, HTTPHelpdeskAdapter(settings, helpdesk)),
                },
            )
            payload = {
                "event_id": "workflow",
                "ticket_id": "T1",
                "customer_email": "example@example.com",
                "message": "Потрібна допомога з PDF",
                "reply_url": "http://mock-helpdesk:8000/tickets/T1/replies?scenario=fail-once",
            }
            assert (await api.post("/webhooks/helpdesk", json=payload)).status_code == 202
            assert await worker.run_once()
            assert searches == [payload["message"]]
            assert not await worker.run_once()  # Nothing can be delivered before review.
            auth = ("operator", "local-demo")
            page = await api.get("/operator/", auth=auth)
            async with sessions() as session:
                generation = (await session.scalars(select(Job))).one()
                job_id = generation.id
            match = re.search(
                rf'action="(/operator/jobs/{job_id}/{action})".*?name="csrf" value="([^"]+)"',
                page.text,
                re.S,
            )
            assert match is not None
            path, token = match.groups()
            for _ in range(2):
                assert (await api.post(path, data={"csrf": token}, auth=auth)).status_code == 303
            if action == "approve":
                assert await worker.run_once()  # Mock's first request returns 503.
                async with sessions.begin() as session:
                    delivery = (
                        await session.scalars(select(Job).where(Job.kind == "delivery"))
                    ).one()
                    assert delivery.state == "pending" and delivery.attempts == 1
                    delivery.available_at = func.clock_timestamp()
                assert await worker.run_once()
            assert not await worker.run_once()
            receipts = await helpdesk.get("http://mock-helpdesk:8000/messages/T1", auth=auth)
            assert receipts.status_code == 200
            assert len(receipts.json()) == (1 if action == "approve" else 0)
            page = await api.get("/operator/", auth=auth)
            if action == "approve":
                assert "Доставка: Готово" in page.text
                assert "http://app/kb/export-pdf" in receipts.json()[0]["message"]
            else:
                assert "Відхилено" in page.text
            duplicate = await api.post("/webhooks/helpdesk", json=payload)
            assert duplicate.json()["status"] == "duplicate"
            assert not await worker.run_once()


async def test_worker_entrypoint_wires_handlers_and_closes_client_on_shutdown(
    engine: AsyncEngine, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None, database_url=database_url, openai_api_key="test-only", worker_parallelism=2
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        session.add(
            HelpdeskEvent(
                event_id="bootstrap",
                ticket_id="T1",
                customer_email="example@example.com",
                message="PDF",
                reply_url="http://mock-helpdesk:8000/tickets/T1/replies",
            )
        )
        await session.flush()
        session.add(Job(event_id="bootstrap", kind="generation"))

    calls = []
    clients = []
    original_client = httpx.AsyncClient

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url == "https://api.openai.com/v1/responses"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {"message": "Експорт → PDF.", "article_ids": ["export-pdf"]}
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    def client_factory(**kwargs) -> httpx.AsyncClient:
        client = original_client(transport=httpx.MockTransport(provider), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("app.worker.get_settings", lambda: settings)
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    task = asyncio.create_task(main())
    try:
        async with asyncio.timeout(5):
            while True:
                async with sessions() as session:
                    job = (await session.scalars(select(Job))).one()
                    if job.state == "succeeded":
                        assert "Експорт → PDF" in job.result["message"]
                        break
                if task.done():
                    await task
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) == 1
    assert clients and all(client.is_closed for client in clients)
