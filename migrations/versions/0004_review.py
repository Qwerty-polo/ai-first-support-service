"""Operator decisions and immutable delivery payload."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_review"
down_revision = "0003_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "helpdesk_events",
        sa.Column("decision", sa.String(20), server_default="pending", nullable=False),
    )
    op.add_column(
        "helpdesk_events", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_event_decision", "helpdesk_events", "decision IN ('pending', 'approved', 'rejected')"
    )
    op.add_column("jobs", sa.Column("payload", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "payload")
    op.drop_constraint("ck_event_decision", "helpdesk_events", type_="check")
    op.drop_column("helpdesk_events", "reviewed_at")
    op.drop_column("helpdesk_events", "decision")
