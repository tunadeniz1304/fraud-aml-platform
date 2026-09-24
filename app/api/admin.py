"""Admin endpoints: account overrides and compliance exports.

Every mutation is written to the hash-chained audit log. Manual status changes
are a human decision (``actor=<username>``); from F4 on, lifting a block goes
through the maker-checker approval flow.
"""

from __future__ import annotations

import csv
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.api.deps import require_pipeline
from app.api.schemas import AccountOut, AuditRowOut
from app.api.state import state
from app.core.account_state import STATUSES
from app.db import repository as repo
from app.security.auth import Principal
from app.security.deps import require_role
from app.services.accounts import AccountNotFoundError

logger = logging.getLogger("fraud.admin")

router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))]
)

AUDIT_COLS = (
    "id",
    "created_at",
    "transaction_id",
    "customer_id",
    "risk_score",
    "decision",
    "reason",
    "actor",
    "event_type",
    "hash",
)


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts() -> list[AccountOut]:
    if state.pipeline is None:
        return []
    return [AccountOut(**a) for a in await state.pipeline.accounts.list_accounts()]


@router.post("/accounts/{customer_id}/status", response_model=AccountOut)
async def set_status(
    customer_id: str,
    status_: str = Query(..., alias="status"),
    principal: Principal = Depends(require_role("admin")),
) -> AccountOut:
    """Manually set an account's hesap_durumu (AKTIF|BLOKE|INCELENIYOR)."""
    pipeline = require_pipeline()
    normalized = status_.strip().upper()
    if normalized not in STATUSES:
        raise HTTPException(status_code=422, detail="Geçerli: AKTIF, BLOKE, INCELENIYOR")
    try:
        await pipeline.accounts.set_by_analyst(
            customer_id,
            normalized,
            actor=principal.username,
            reason="Yönetici tarafından manuel durum değişikliği",
        )
    except AccountNotFoundError:
        raise HTTPException(status_code=404, detail="Müşteri bulunamadı") from None
    account = await pipeline.accounts.get_account(customer_id)
    return AccountOut(**account)  # type: ignore[arg-type]


@router.get("/audit", response_model=list[AuditRowOut])
async def audit(limit: int = Query(50, ge=1, le=1000)) -> list[AuditRowOut]:
    if state.pipeline is None:
        return []
    async with state.pipeline.db.session() as session:
        return [AuditRowOut(**r) for r in await repo.list_audit(session, limit)]


def audit_csv(rows: list[dict[str, object]]) -> str:
    """RFC-4180 CSV (all fields quoted)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(AUDIT_COLS)
    for r in rows:
        writer.writerow([r.get(k, "") for k in AUDIT_COLS])
    return buffer.getvalue()


@router.get("/audit/export", response_class=PlainTextResponse)
async def export_audit() -> str:
    """Compliance export of the audit log."""
    if state.pipeline is None:
        return audit_csv([])
    await state.pipeline.writer.flush()
    async with state.pipeline.db.session() as session:
        rows = await repo.list_audit(session, limit=50_000)
    return audit_csv(rows)
