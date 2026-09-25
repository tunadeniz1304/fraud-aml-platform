"""one alert per transaction (A11)

Existing duplicates (case-outbox replays before this revision) are removed
first, keeping the oldest alert of each transaction, and the counters of the
affected cases are recomputed from their remaining alerts.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUPLICATES = sa.text(
    "SELECT a.id, a.case_id FROM alerts a WHERE EXISTS ("
    " SELECT 1 FROM alerts b WHERE b.transaction_id = a.transaction_id AND b.id < a.id)"
)
_RECOUNT = sa.text(
    "UPDATE cases SET"
    " alert_count = (SELECT COUNT(*) FROM alerts WHERE alerts.case_id = cases.id),"
    " total_amount_try = (SELECT COALESCE(SUM(amount_try), 0) FROM alerts"
    "  WHERE alerts.case_id = cases.id),"
    " priority = (SELECT COALESCE(ROUND(CAST(SUM(risk_score * amount_try) AS NUMERIC), 2), 0)"
    "  FROM alerts WHERE alerts.case_id = cases.id)"
    " WHERE id = :case_id"
)


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(_DUPLICATES).fetchall()
    if duplicates:
        ids = [row[0] for row in duplicates]
        cases = {row[1] for row in duplicates if row[1] is not None}
        alerts = sa.table("alerts", sa.column("id", sa.Integer))
        for start in range(0, len(ids), 500):
            bind.execute(sa.delete(alerts).where(alerts.c.id.in_(ids[start : start + 500])))
        for case_id in sorted(cases):
            bind.execute(_RECOUNT, {"case_id": case_id})
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_alerts_transaction_id"))
        batch_op.create_index(
            batch_op.f("ix_alerts_transaction_id"), ["transaction_id"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_alerts_transaction_id"))
        batch_op.create_index(
            batch_op.f("ix_alerts_transaction_id"), ["transaction_id"], unique=False
        )
