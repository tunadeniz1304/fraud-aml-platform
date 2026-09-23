"""FastAPI live dashboard — real-time status REST endpoints (Adım 5 alt yapı).

Exposes the running pipeline state read via injected references; the dashboard
never owns agents, so the same EventBus/agents can serve both the simulation
and the HTTP API. Health, status, transactions, blocks, accounts, audit log,
and a live ingest endpoint (strict Pydantic validation) for the dashboard.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse

from app import config
from app.api.admin import router as admin_router
from app.api.frontend import render as render_frontend
from app.api.schemas import (
    AccountOut,
    AnalyzedTransactionOut,
    AuditRowOut,
    BlockOut,
    PipelineStatusOut,
    TransactionIn,
)
from app.api.state import state

logger = logging.getLogger("fraud.dashboard")


app = FastAPI(
    title="Siber Güvenlik ve Fraud Ajanı — Dashboard",
    version="1.0.0",
    description="Gerçek zamanlı dolandırıcılık analiz ajanı için durum izleme API'si.",
)
app.include_router(admin_router)


@app.get("/", response_class=HTMLResponse, tags=["ui"])
async def index() -> str:
    return render_frontend()


@app.get("/api/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "llm_provider": config.settings.llm_provider}


@app.get("/api/status", response_model=PipelineStatusOut, tags=["pipeline"])
async def status() -> PipelineStatusOut:
    _require_wired()
    accounts = [AccountOut(**a) for a in state.store.list_accounts()] if state.store else []
    return PipelineStatusOut(
        total_monitored=len(state.monitor.monitored),
        analyzed=len(state.analyst.analyzed),
        blocked=len(state.action.blocked),
        warned=len(state.action.warned),
        passed=len(state.action.passed),
        llm_provider=config.settings.llm_provider,
        vector_customers=state.vector.count if state.vector else 0,
        accounts=accounts,
    )


@app.get("/api/stats", tags=["pipeline"])
async def stats() -> dict[str, object]:
    """Online risk-score distribution (drift visibility) across analyzed events."""
    _require_wired()
    return state.analyst.stats.summary()


@app.get("/api/transactions", response_model=list[AnalyzedTransactionOut], tags=["pipeline"])
async def transactions() -> list[AnalyzedTransactionOut]:
    _require_wired()
    return [AnalyzedTransactionOut(**a) for a in state.analyst.analyzed]


@app.get("/api/blocks", response_model=list[BlockOut], tags=["pipeline"])
async def blocks() -> list[BlockOut]:
    _require_wired()
    return [BlockOut(**b) for b in state.action.blocked]


@app.get("/api/accounts", response_model=list[AccountOut], tags=["accounts"])
async def accounts() -> list[AccountOut]:
    if state.store is None:
        return []
    return [AccountOut(**a) for a in state.store.list_accounts()]


@app.get("/api/audit", response_model=list[AuditRowOut], tags=["accounts"])
async def audit(limit: int = 100) -> list[AuditRowOut]:
    if state.store is None:
        return []
    return [AuditRowOut(**r) for r in state.store.list_audit(limit=limit)]


@app.post("/api/transactions", response_model=AnalyzedTransactionOut, tags=["pipeline"])
async def ingest(tx: TransactionIn) -> AnalyzedTransactionOut:
    """Live-ingest a transaction into the running pipeline (strictly validated)."""
    _require_wired()
    payload = tx.model_dump(mode="json")
    payload["ts"] = tx.ts.isoformat()
    from app.agents.transaction_monitor import TransactionMonitor

    await state.bus.publish(TransactionMonitor.CREATED, payload)
    analyzed = next(
        (a for a in reversed(state.analyst.analyzed) if a["transaction_id"] == tx.transaction_id),
        None,
    )
    if analyzed is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="İşlem analiz edilemedi",
        )
    return AnalyzedTransactionOut(**analyzed)


def _require_wired() -> None:
    if not state.wired:
        raise HTTPException(status_code=503, detail="Pipeline henüz bağlanmadı")
