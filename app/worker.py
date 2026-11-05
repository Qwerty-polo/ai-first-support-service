import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.errors import PermanentError, RetryableError
from app.queue import HANDLER_TIMEOUT, Claim, Queue

logger = logging.getLogger(__name__)
Handler = Callable[[Claim], Awaitable[dict[str, Any]]]


class Worker:
    def __init__(self, queue: Queue, handlers: dict[str, Handler]) -> None:
        self.queue = queue
        self.handlers = handlers

    async def run_once(self) -> bool:
        claim = await self.queue.claim(tuple(self.handlers))
        if claim is None:
            return False
        try:
            async with asyncio.timeout(HANDLER_TIMEOUT):
                result = await self.handlers[claim.kind](claim)
        except (TimeoutError, RetryableError):
            await self.queue.fail(claim, "temporary_handler_failure", retryable=True)
        except PermanentError:
            await self.queue.fail(claim, "permanent_handler_failure", retryable=False)
        except Exception:
            await self.queue.fail(claim, "unexpected_handler_failure", retryable=False)
        else:
            await self.queue.finish(claim, result)
        # Cancellation/crash leaves the lease in place for another worker to recover.
        return True

    async def run(self) -> None:
        while True:
            try:
                worked = await self.run_once()
            except (SQLAlchemyError, OSError, TimeoutError):
                logger.warning("Worker storage unavailable; retrying")
                worked = False
            if not worked:
                await asyncio.sleep(1)


async def main() -> None:
    import httpx

    from app.delivery import DeliveryHandler, HTTPHelpdeskAdapter
    from app.generation import GenerationHandler
    from app.llm import OpenAIAdapter

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    # Request URLs may carry helpdesk query secrets; keep HTTP client logs out of production output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            sessions = create_session_factory(engine)
            handler = GenerationHandler(
                sessions, OpenAIAdapter(settings, client), str(settings.public_base_url)
            )
            delivery = DeliveryHandler(sessions, HTTPHelpdeskAdapter(settings, client))
            worker = Worker(Queue(sessions), handlers={"generation": handler, "delivery": delivery})
            logger.info("Worker ready; generation adapter configured")
            async with asyncio.TaskGroup() as group:
                for _ in range(settings.worker_parallelism):
                    group.create_task(worker.run())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
