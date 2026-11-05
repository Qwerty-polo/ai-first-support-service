import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.queue import HANDLER_TIMEOUT, Claim, Queue

logger = logging.getLogger(__name__)
Handler = Callable[[Claim], Awaitable[dict[str, Any]]]


class RetryableError(Exception):
    """A temporary provider failure. Exception messages must not be logged."""


class PermanentError(Exception):
    """A request that should not be retried."""


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
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    engine = create_engine(settings)
    # Register real adapters in the next stage. Unknown jobs must remain pending.
    worker = Worker(Queue(create_session_factory(engine)), handlers={})
    logger.info("Worker ready; generation adapter is not configured yet")
    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(settings.worker_parallelism):
                group.create_task(worker.run())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
