"""Durable mock helpdesk receipt deduplication and fault counters."""

import sqlalchemy as sa
from alembic import op

revision = "0005_mock_receipts"
down_revision = "0004_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mock_attempts",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_table(
        "mock_receipts",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("ticket_id", sa.String(200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_mock_receipts_ticket_id", "mock_receipts", ["ticket_id"])


def downgrade() -> None:
    op.drop_table("mock_receipts")
    op.drop_table("mock_attempts")
