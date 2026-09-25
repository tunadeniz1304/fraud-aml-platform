"""durability: dead letters, runtime config, case outbox, payload hash

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import app.db.base
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "dead_letters",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("resolved_at", app.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dead_letters")),
    )
    with op.batch_alter_table("dead_letters", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_dead_letters_created_at"), ["created_at"], unique=False)
    op.create_table(
        "runtime_config",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", _JSON, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_runtime_config")),
    )
    op.create_table(
        "case_outbox",
        sa.Column("transaction_id", sa.String(length=64), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("transaction_id", name=op.f("pk_case_outbox")),
    )
    with op.batch_alter_table("case_outbox", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_case_outbox_created_at"), ["created_at"], unique=False)
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("payload_hash", sa.String(length=64), nullable=True))
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_accounts_updated_at"), ["updated_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_accounts_updated_at"))
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_column("payload_hash")
    with op.batch_alter_table("case_outbox", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_case_outbox_created_at"))
    op.drop_table("case_outbox")
    op.drop_table("runtime_config")
    with op.batch_alter_table("dead_letters", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_dead_letters_created_at"))
    op.drop_table("dead_letters")
