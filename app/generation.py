from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import PermanentError
from app.knowledge import ARTICLES, Article, search_articles
from app.llm import DraftGenerator, validate_sources
from app.models import HelpdeskEvent
from app.queue import Claim


class GenerationHandler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        generator: DraftGenerator,
        public_base_url: str = "http://localhost:8000",
        *,
        search: Callable[[str], list[Article]] = search_articles,
    ) -> None:
        self.sessions = sessions
        self.generator = generator
        self.search = search
        self.public_base_url = public_base_url.rstrip("/")

    async def __call__(self, claim: Claim) -> dict[str, Any]:
        async with self.sessions() as session:
            event = await session.get(HelpdeskEvent, claim.event_id)
            if event is None:
                raise PermanentError("event_missing")
            message = event.message
        articles = self.search(message)
        needs_clarification = not articles
        if needs_clarification:
            # An escalation article supports clarification without inventing a solution.
            articles = [ARTICLES[-1]]
        candidate = validate_sources(await self.generator.generate(message, articles), articles)
        sources = [
            {"id": a.id, "title": a.title, "url": self.public_base_url + a.url}
            for a in articles
            if a.id in candidate.article_ids
        ]
        links = "\n".join(f"{a['title']}: {a['url']}" for a in sources)
        return {
            "message": candidate.message + "\n\nДжерела:\n" + links,
            "sources": sources,
            "needs_clarification": needs_clarification,
        }
