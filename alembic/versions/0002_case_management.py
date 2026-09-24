"""case management columns (F4)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("amount_try", sa.Numeric(18, 2), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("decision", sa.String(length=12), server_default="", nullable=False)
        )
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("title", sa.String(length=200), server_default="", nullable=False)
        )
        batch_op.add_column(
            sa.Column("alert_count", sa.Integer(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.drop_column("alert_count")
        batch_op.drop_column("title")
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_column("decision")
        batch_op.drop_column("amount_try")
