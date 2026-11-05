from dataclasses import dataclass
from datetime import timedelta
from random import uniform
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Job, LLMAdmission, LLMPolicy

MAX_ATTEMPTS = 3
LEASE_SECONDS = 60
HANDLER_TIMEOUT = 40


@dataclass(frozen=True)
class Claim:
    id: UUID
    event_id: str
    kind: str
    token: UUID
    attempt: int


class Queue:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def claim(self, kinds: tuple[str, ...]) -> Claim | None:
        if not kinds:
            return None
        async with self.sessions.begin() as session:
            # All workers serialize admission through one DB row, including retries.
            # This lock is released before any handler/network operation starts.
            policy = (await session.scalars(select(LLMPolicy).with_for_update())).one()
            now = await session.scalar(select(func.clock_timestamp()))
            expired = and_(Job.state == "running", Job.lease_until <= now)
            await session.execute(
                update(Job)
                .where(expired, Job.attempts >= MAX_ATTEMPTS)
                .values(
                    state="failed",
                    lease_token=None,
                    lease_until=None,
                    last_error="lease_expired",
                )
            )
            await session.execute(
                delete(LLMAdmission).where(LLMAdmission.admitted_at <= now - timedelta(seconds=60))
            )
            active = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "generation", Job.state == "running", Job.lease_until > now)
            )
            admissions = await session.scalar(select(func.count()).select_from(LLMAdmission))
            eligible_kinds = list(kinds)
            if active >= policy.concurrency or admissions >= 60:
                eligible_kinds = [kind for kind in kinds if kind != "generation"]
            if not eligible_kinds:
                return None
            job = await session.scalar(
                select(Job)
                .where(
                    Job.kind.in_(eligible_kinds),
                    Job.attempts < MAX_ATTEMPTS,
                    or_(and_(Job.state == "pending", Job.available_at <= now), expired),
                )
                .order_by(Job.available_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.state = "running"
            job.attempts += 1
            job.lease_token = uuid4()
            job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            if job.kind == "generation":
                session.add(LLMAdmission(admitted_at=now))
            return Claim(job.id, job.event_id, job.kind, job.lease_token, job.attempts)

    async def finish(self, claim: Claim, result: dict[str, Any]) -> bool:
        return await self._write(claim, state="succeeded", result=result, last_error=None)

    async def fail(self, claim: Claim, code: str, *, retryable: bool) -> bool:
        retry = retryable and claim.attempt < MAX_ATTEMPTS
        # Jitter spreads retries after a provider outage; attempts remain bounded.
        delay = uniform(2**claim.attempt, 2 ** (claim.attempt + 1))
        return await self._write(
            claim,
            state="pending" if retry else "failed",
            last_error=code,
            available_at=func.clock_timestamp() + timedelta(seconds=delay),
        )

    async def _write(self, claim: Claim, **values: Any) -> bool:
        async with self.sessions.begin() as session:
            # Token + unexpired lease fence out results from crashed/stale workers.
            saved = await session.scalar(
                update(Job)
                .where(
                    Job.id == claim.id,
                    Job.state == "running",
                    Job.lease_token == claim.token,
                    Job.lease_until > func.clock_timestamp(),
                )
                .values(**values, lease_token=None, lease_until=None)
                .returning(Job.id)
            )
            return saved is not None
