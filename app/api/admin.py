"""Admin / case-management endpoints for the fraud dashboard.

Typical of real fraud-ops consoles: a human analyst can query account status,
manually (un)block an account, and dump the audit log. All mutations write an
audit_log row so every decision stays traceable (hesap_durumu + denetim logu
requirement).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.api.schemas import AccountOut, AuditRowOut
from app.api.state import state

logger = logging.getLogger("fraud.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts() -> list[AccountOut]:
    if state.store is None:
        return []
    accounts = state.store.list_accounts()
    return [AccountOut(**a) for a in accounts]


@router.post("/accounts/{customer_id}/status", response_model=AccountOut)
async def set_status(customer_id: str, status_: str = Query(..., alias="status")) -> AccountOut:
    """Manually set an account's hesap_durumu (AKTIF|BLOKE|INCELENIYOR).

    Every change is appended to the audit log for traceability.
    """
    if state.store is None:
        raise HTTPException(status_code=503, detail="Depo bağlı değil")
    normalized = status_.strip().upper()
    if normalized not in ("AKTIF", "BLOKE", "INCELENIYOR"):
        raise HTTPException(
            status_code=422,
            detail="Geçerli: AKTIF, BLOKE, INCELENIYOR",
        )
    account = state.store.get_account(customer_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Müşteri bulunamadı")
    previous = account["hesap_durumu"]
    state.store.set_hesap_durumu(customer_id, normalized)
    state.store.append_audit(
        transaction_id="MANUAL",
        customer_id=customer_id,
        risk_score=0.0,
        decision=f"STATUS:{previous}->{normalized}",
        reason="Operatör tarafından manuel durum değişikliği",
    )
    return AccountOut(**state.store.get_account(customer_id))  # type: ignore[arg-type]


@router.get("/audit", response_model=list[AuditRowOut])
async def audit(limit: int = Query(50, ge=1, le=1000)) -> list[AuditRowOut]:
    if state.store is None:
        return []
    return [AuditRowOut(**r) for r in state.store.list_audit(limit=limit)]


@router.get("/audit/export", response_class=PlainTextResponse)
async def export_audit() -> str:
    """RFC-4180 CSV dump of the audit log (compliance export)."""
    cols = ("id", "created_at", "transaction_id", "customer_id", "risk_score", "decision", "reason")

    def row(values) -> str:
        return ",".join(f'"{str(v).replace(chr(34), chr(34) * 2)}"' for v in values)

    if state.store is None:
        return row(cols) + "\n"
    rows = state.store.list_audit(limit=5000)
    lines = [row(cols)]
    for r in rows:
        lines.append(row(tuple(r[k] for k in cols)))
    return "\n".join(lines) + "\n"
