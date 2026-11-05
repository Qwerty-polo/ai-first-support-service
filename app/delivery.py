import asyncio
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import PermanentError, RetryableError
from app.models import HelpdeskEvent, Job
from app.queue import Claim
from app.reply_urls import reply_url_allowed


class HelpdeskSender(Protocol):
    async def send(self, url: str, ticket_id: str, message: str, key: str) -> None: ...


class HTTPHelpdeskAdapter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def send(self, url: str, ticket_id: str, message: str, key: str) -> None:
        # Recheck persisted URLs before sending; never follow redirects outside the allowlist.
        if not reply_url_allowed(url, self.settings.allowed_reply_origins):
            raise PermanentError("reply_url_not_allowed")
        token = self.settings.helpdesk_api_token.get_secret_value()
        try:
            async with asyncio.timeout(5):
                response = await self.client.post(
                    url,
                    json={"ticket_id": ticket_id, "message": message},
                    headers={
                        "Idempotency-Key": key,
                        "Authorization": f"Bearer {token}",
                    },
                    timeout=5,
                    follow_redirects=False,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            # Timeout is ambiguous: repeat only with the same durable idempotency key.
            raise RetryableError("helpdesk_network_error") from exc
        if response.status_code == 201:
            return
        if response.status_code in (408, 429) or response.status_code >= 500:
            raise RetryableError("helpdesk_temporary_error")
        raise PermanentError("helpdesk_rejected_request")


class DeliveryHandler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], sender: HelpdeskSender) -> None:
        self.sessions = sessions
        self.sender = sender

    async def __call__(self, claim: Claim) -> dict[str, Any]:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(Job, HelpdeskEvent)
                    .join(HelpdeskEvent)
                    .where(
                        Job.id == claim.id,
                        Job.kind == "delivery",
                        Job.state == "running",
                        Job.lease_token == claim.token,
                        Job.lease_until > func.clock_timestamp(),
                        HelpdeskEvent.decision == "approved",
                    )
                )
            ).one_or_none()
            if row is None:
                raise PermanentError("delivery_not_authorized")
            job, event = row
            message = (job.payload or {}).get("message")
            if not isinstance(message, str) or not message.strip():
                raise PermanentError("delivery_payload_missing")
            url, ticket_id = event.reply_url, event.ticket_id
        # Job UUID survives retries/reclaims; lease token must never be used as an HTTP key.
        await self.sender.send(url, ticket_id, message, f"delivery:{claim.id}")
        return {"status": "sent", "http_status": 201}
