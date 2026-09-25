"""one pending approval per (kind, target_id)

A database that already holds several pending requests for one target
(created before this guard) would fail the unique index, so the older
duplicates are first closed as REDDEDILDI by ``system:migration-0003``
(the newest request per target stays pending) -- L10.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op
from app.db.base import UTCDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PENDING = sa.text("status = 'BEKLIYOR'")
_SUPERSEDE = sa.text(
    "UPDATE approvals SET status = 'REDDEDILDI',"
    " decided_by = 'system:migration-0003', decided_at = :now,"
    " note = COALESCE(note, '') || :why"
    " WHERE status = 'BEKLIYOR' AND EXISTS ("
    "  SELECT 1 FROM approvals newer WHERE newer.kind = approvals.kind"
    "  AND newer.target_id = approvals.target_id AND newer.status = 'BEKLIYOR'"
    "  AND newer.id > approvals.id)"
).bindparams(sa.bindparam("now", type_=UTCDateTime(timezone=True)))


def upgrade() -> None:
    op.get_bind().execute(
        _SUPERSEDE,
        {
            "now": datetime.now(UTC),
            "why": " [yinelenen bekleyen talep: daha yeni talep geçerli]",
        },
    )
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
