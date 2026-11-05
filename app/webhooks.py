import asyncio
import logging
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import HelpdeskEvent, Job
from app.reply_urls import reply_url_allowed

router = APIRouter()
logger = logging.getLogger(__name__)
Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]


class HelpdeskWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: Identifier
    ticket_id: Identifier
    customer_email: Annotated[
        str, Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+$")
    ]
    message: Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
    reply_url: Annotated[HttpUrl, Field(max_length=2048)]

    @field_validator("reply_url")
    @classmethod
    def reject_url_credentials(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("reply_url must not contain credentials")
        return value


class WebhookReceipt(BaseModel):
    event_id: str
    status: Literal["accepted", "duplicate"]


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)


async def persist_event(
    payload: HelpdeskWebhook, factory: async_sessionmaker[AsyncSession]
) -> bool:
    values = payload.model_dump(mode="json")
    async with factory.begin() as session:
        # The unique PK arbitrates concurrent deliveries; a SELECT-before-INSERT would race.
        event_id = await session.scalar(
            insert(HelpdeskEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[HelpdeskEvent.event_id])
            .returning(HelpdeskEvent.event_id)
        )
        if event_id is None:
            existing = (
                (
                    await session.execute(
                        select(
                            HelpdeskEvent.ticket_id,
                            HelpdeskEvent.customer_email,
                            HelpdeskEvent.message,
                            HelpdeskEvent.reply_url,
                        ).where(HelpdeskEvent.event_id == payload.event_id)
                    )
                )
                .mappings()
                .one()
            )
            if dict(existing) != {k: v for k, v in values.items() if k != "event_id"}:
                raise HTTPException(409, "event_id already belongs to a different payload")
            return False
        session.add(Job(event_id=event_id, kind="generation"))
        # Exiting begin() commits both records; any failure rolls back the event as well.
    return True


@router.post("/webhooks/helpdesk", response_model=WebhookReceipt, status_code=202)
async def receive_helpdesk(
    payload: HelpdeskWebhook,
    request: Request,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> WebhookReceipt:
    if not reply_url_allowed(
        str(payload.reply_url), request.app.state.settings.allowed_reply_origins
    ):
        raise HTTPException(422, "reply_url origin is not allowed")
    try:
        # Leave headroom under the helpdesk's 2-second timeout; no provider calls here.
        async with asyncio.timeout(1.5):
            created = await persist_event(payload, factory)
    except (TimeoutError, SQLAlchemyError, OSError):
        # DB exceptions can embed SQL parameters (including customer text); never log them.
        logger.warning("Webhook persistence unavailable")
        raise HTTPException(
            503, "Storage unavailable; retry delivery", headers={"Retry-After": "1"}
        ) from None
    return WebhookReceipt(event_id=payload.event_id, status="accepted" if created else "duplicate")
