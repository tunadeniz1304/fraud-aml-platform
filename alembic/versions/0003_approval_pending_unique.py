"""one pending approval per (kind, target_id)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PENDING = sa.text("status = 'BEKLIYOR'")


def upgrade() -> None:
    op.create_index(
        "uq_approvals_pending_target",
        "approvals",
        ["kind", "target_id"],
        unique=True,
        sqlite_where=_PENDING,
        postgresql_where=_PENDING,
    )


def downgrade() -> None:
    op.drop_index("uq_approvals_pending_target", table_name="approvals")
