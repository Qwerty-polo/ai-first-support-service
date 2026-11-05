"""Persist webhook events and generation jobs atomically."""

import sqlalchemy as sa
from alembic import op

revision: str = "0002_events_jobs"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "helpdesk_events",
        sa.Column("event_id", sa.String(200), primary_key=True),
        sa.Column("ticket_id", sa.String(200), nullable=False),
        sa.Column("customer_email", sa.String(320), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reply_url", sa.String(2048), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "event_id", sa.String(200), sa.ForeignKey("helpdesk_events.event_id"), nullable=False
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("event_id", "kind", name="uq_jobs_event_kind"),
        sa.CheckConstraint("kind IN ('generation', 'delivery')", name="ck_jobs_kind"),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed')", name="ck_jobs_state"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts"),
    )
    op.create_index("ix_jobs_ready", "jobs", ["state", "available_at"])


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("helpdesk_events")
