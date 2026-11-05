import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.models import MockAttempt, MockReceipt
from app.operator import require_operator


class Reply(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=20000)


def create_mock_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(config)
        application.state.sessions = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    application = FastAPI(title="Mock Helpdesk", lifespan=lifespan)
    application.state.settings = config

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/tickets/{ticket_id}/replies", status_code=201)
    async def receive(
        ticket_id: str,
        body: Reply,
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=100)],
        authorization: Annotated[str, Header()] = "",
        scenario: Literal[
            "normal", "fail-once", "always-500", "timeout-before", "accept-timeout"
        ] = "normal",
    ) -> dict[str, str]:
        expected = "Bearer " + config.helpdesk_api_token.get_secret_value()
        if not config.helpdesk_api_token.get_secret_value() or not secrets.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(401, "Invalid helpdesk token")
        if ticket_id != body.ticket_id:
            raise HTTPException(422, "Ticket mismatch")
        async with request.app.state.sessions.begin() as session:
            attempt = await session.scalar(
                insert(MockAttempt)
                .values(key=idempotency_key, attempts=1)
                .on_conflict_do_update(
                    index_elements=[MockAttempt.key], set_={"attempts": MockAttempt.attempts + 1}
                )
                .returning(MockAttempt.attempts)
            )
        if scenario == "always-500" or (scenario == "fail-once" and attempt == 1):
            raise HTTPException(503, "Simulated temporary failure")
        if scenario == "timeout-before" and attempt == 1:
            await asyncio.sleep(8)
            raise HTTPException(503, "Simulated timeout before acceptance")
        async with request.app.state.sessions.begin() as session:
            # This record IS the simulated customer-visible side effect. Both payload and
            # key commit together; even concurrent requests can accept only one message.
            inserted = await session.scalar(
                insert(MockReceipt)
                .values(
                    key=idempotency_key,
                    ticket_id=ticket_id,
                    message=body.message,
                )
                .on_conflict_do_nothing(index_elements=[MockReceipt.key])
                .returning(MockReceipt.key)
            )
            if inserted is None:
                existing = await session.get(MockReceipt, idempotency_key)
                if existing.ticket_id != ticket_id or existing.message != body.message:
                    raise HTTPException(409, "Idempotency key payload conflict")
        # Delay after COMMIT deliberately loses the acknowledgment, not the accepted reply.
        if scenario == "accept-timeout" and inserted is not None:
            await asyncio.sleep(8)
        return {"status": "accepted"}

    @application.get("/messages/{ticket_id}", dependencies=[Depends(require_operator)])
    async def messages(ticket_id: str, request: Request) -> list[dict[str, str]]:
        async with request.app.state.sessions() as session:
            receipts = (
                await session.scalars(
                    select(MockReceipt)
                    .where(MockReceipt.ticket_id == ticket_id)
                    .order_by(MockReceipt.created_at)
                )
            ).all()
        return [{"ticket_id": r.ticket_id, "message": r.message} for r in receipts]

    return application


app = create_mock_app()
