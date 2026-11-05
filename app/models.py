from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class HelpdeskEvent(Base):
    __tablename__ = "helpdesk_events"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('pending', 'approved', 'rejected')", name="ck_event_decision"
        ),
    )

    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(200))
    customer_email: Mapped[str] = mapped_column(String(320))
    message: Mapped[str] = mapped_column(Text)
    reply_url: Mapped[str] = mapped_column(String(2048))
    decision: Mapped[str] = mapped_column(String(20), server_default="pending")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("now()")
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("event_id", "kind", name="uq_jobs_event_kind"),
        CheckConstraint("kind IN ('generation', 'delivery')", name="ck_jobs_kind"),
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed')", name="ck_jobs_state"
        ),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts"),
        Index("ix_jobs_ready", "state", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[str] = mapped_column(ForeignKey("helpdesk_events.event_id"))
    kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempts: Mapped[int] = mapped_column(server_default="0")
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("now()")
    )


class LLMPolicy(Base):
    __tablename__ = "llm_policy"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_llm_policy_singleton"),
        CheckConstraint("concurrency BETWEEN 1 AND 60", name="ck_llm_concurrency"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    concurrency: Mapped[int] = mapped_column(server_default="32")


class LLMAdmission(Base):
    __tablename__ = "llm_admissions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class MockAttempt(Base):
    __tablename__ = "mock_attempts"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    attempts: Mapped[int] = mapped_column(server_default="1")


class MockReceipt(Base):
    __tablename__ = "mock_receipts"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(200), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("now()")
    )
