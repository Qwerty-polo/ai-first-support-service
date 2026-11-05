import asyncio
from datetime import timedelta
from time import perf_counter

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import Settings
from app.generation import GenerationHandler
from app.knowledge import Article
from app.llm import Candidate
from app.main import create_app
from app.models import HelpdeskEvent, Job, LLMAdmission
from app.queue import Queue
from app.worker import Worker

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_500_events_are_durable_and_drained_with_global_budget(
    engine: AsyncEngine,
    database_url: str,
) -> None:
    app = create_app(Settings(_env_file=None, database_url=database_url))
    latencies: list[float] = []
    gate = asyncio.Semaphore(10)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://load") as client:

            async def deliver(index: int) -> None:
                async with gate:
                    started = perf_counter()
                    response = await client.post(
                        "/webhooks/helpdesk",
                        json={
                            "event_id": f"capacity-{index}",
                            "ticket_id": f"T-{index}",
                            "customer_email": "load@example.com",
                            "message": "Експорт звіту PDF",
                            "reply_url": f"http://mock-helpdesk:8000/tickets/T-{index}/replies",
                        },
                    )
                    latencies.append(perf_counter() - started)
                    assert response.status_code == 202

            await asyncio.gather(
                *(deliver(i) for i in range(500)), *(deliver(i) for i in range(50))
            )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(HelpdeskEvent)) == 500
        assert await s.scalar(select(func.count()).select_from(Job)) == 500
    assert max(latencies) < 2
    print(f"550 requests / 500 unique: p95={sorted(latencies)[522]:.3f}s max={max(latencies):.3f}s")

    calls = 0

    class ControlledLLM:
        async def generate(self, message: str, articles: list[Article]) -> Candidate:
            nonlocal calls
            calls += 1
            return Candidate(message="Експорт → PDF.", article_ids=[articles[0].id])

    generator = ControlledLLM()
    workers = [
        Worker(Queue(sessions), {"generation": GenerationHandler(sessions, generator)})
        for _ in range(2)
    ]
    for _ in range(9):
        before = calls
        while any(await asyncio.gather(*(workers[i % 2].run_once() for i in range(4)))):
            pass
        assert calls - before == min(60, 500 - before)
        async with sessions.begin() as s:
            succeeded = await s.scalar(
                select(func.count()).select_from(Job).where(Job.state == "succeeded")
            )
            pending = await s.scalar(
                select(func.count()).select_from(Job).where(Job.state == "pending")
            )
            assert succeeded + pending == 500
            # Advance only the isolated test DB's admission clock; no real 9-minute sleep.
            await s.execute(
                update(LLMAdmission).values(
                    admitted_at=func.clock_timestamp() - timedelta(seconds=61)
                )
            )
        if calls == 500:
            break
    assert calls == 500 and succeeded == 500
