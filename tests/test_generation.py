import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.generation import GenerationHandler
from app.knowledge import Article
from app.llm import Candidate
from app.models import HelpdeskEvent, Job
from app.queue import Queue
from app.worker import Worker

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize(
    "message,source,clarify",
    [
        ("Не можу експортувати звіт у PDF", "export-pdf", False),
        ("квантовий телепортатор", "contact-support", True),
    ],
)
async def test_worker_persists_validated_draft_with_real_links(
    engine: AsyncEngine,
    message: str,
    source: str,
    clarify: bool,
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        session.add(
            HelpdeskEvent(
                event_id="generation-test",
                ticket_id="T1",
                customer_email="user@example.com",
                message=message,
                reply_url="http://mock/reply",
            )
        )
        await session.flush()
        session.add(Job(event_id="generation-test", kind="generation"))

    class ControlledGenerator:
        async def generate(self, message: str, articles: list[Article]) -> Candidate:
            assert articles[0].id == source
            return Candidate(message="Відповідь оператора.", article_ids=[source])

    handler = GenerationHandler(sessions, ControlledGenerator(), "http://localhost:8001")
    assert await Worker(Queue(sessions), {"generation": handler}).run_once()
    async with sessions() as session:
        job = (await session.scalars(select(Job))).one()
        assert job.state == "succeeded"
        assert job.result["needs_clarification"] is clarify
        assert job.result["sources"][0]["url"] == f"http://localhost:8001/kb/{source}"
        assert job.result["sources"][0]["url"] in job.result["message"]
