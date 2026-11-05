"""Worker ownership, results and global LLM admission control."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_worker"
down_revision = "0002_events_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("lease_token", sa.Uuid(), nullable=True))
    op.add_column("jobs", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("last_error", sa.String(80), nullable=True))
    op.add_column("jobs", sa.Column("result", postgresql.JSONB(), nullable=True))
    op.create_table(
        "llm_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("concurrency", sa.Integer(), server_default="32", nullable=False),
        sa.CheckConstraint("id = 1", name="ck_llm_policy_singleton"),
        sa.CheckConstraint("concurrency BETWEEN 1 AND 60", name="ck_llm_concurrency"),
    )
    op.execute("INSERT INTO llm_policy (id, concurrency) VALUES (1, 32)")
    op.create_table(
        "llm_admissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_admissions_admitted_at", "llm_admissions", ["admitted_at"])


def downgrade() -> None:
    op.drop_table("llm_admissions")
    op.drop_table("llm_policy")
    for name in ("result", "last_error", "lease_until", "lease_token"):
        op.drop_column("jobs", name)
