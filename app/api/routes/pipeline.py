"""Pipeline read endpoints + authenticated live ingest."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status

from app.api.deps import require_pipeline
from app.api.schemas import (
    AccountOut,
    AnalyzedTransactionOut,
    AuditRowOut,
    BlockOut,
    PipelineStatusOut,
    TransactionIn,
)
from app.db import repository as repo
from app.db.audit import verify_chain
from app.security.auth import Principal
from app.security.deps import ingest_principal, require_role
from app.security.ratelimit import ingest_limit, limiter

router = APIRouter(prefix="/api", tags=["pipeline"])
analyst_only = Depends(require_role("analist"))


@router.get("/status", response_model=PipelineStatusOut, dependencies=[analyst_only])
async def status() -> PipelineStatusOut:
    pipeline = require_pipeline()
    accounts = [AccountOut(**a) for a in await pipeline.accounts.list_accounts()]
    return PipelineStatusOut(
        total_monitored=len(pipeline.monitor.monitored),
        rejected=len(pipeline.monitor.rejected),
        analyzed=len(pipeline.analyst.analyzed),
        blocked=len(pipeline.action.blocked),
        warned=len(pipeline.action.warned),
        passed=len(pipeline.action.passed),
        llm_mode=pipeline.llm.mode,
        vector_customers=pipeline.vector.count if pipeline.vector else 0,
        accounts=accounts,
    )


@router.get("/stats", dependencies=[analyst_only])
async def stats() -> dict[str, object]:
    """Online risk-score distribution (drift visibility) across analyzed events."""
    return require_pipeline().analyst.stats.summary()


@router.get(
    "/transactions", response_model=list[AnalyzedTransactionOut], dependencies=[analyst_only]
)
async def transactions(limit: int = Query(200, ge=1, le=5000)) -> list[AnalyzedTransactionOut]:
    analyzed = list(require_pipeline().analyst.analyzed)[-limit:]
    return [AnalyzedTransactionOut(**a) for a in analyzed]


@router.get("/blocks", response_model=list[BlockOut], dependencies=[analyst_only])
async def blocks() -> list[BlockOut]:
    return [BlockOut(**b) for b in require_pipeline().action.blocked]


@router.get("/accounts", response_model=list[AccountOut], dependencies=[analyst_only])
async def accounts() -> list[AccountOut]:
    pipeline = require_pipeline()
    return [AccountOut(**a) for a in await pipeline.accounts.list_accounts()]


@router.get("/audit", response_model=list[AuditRowOut], dependencies=[analyst_only])
async def audit(limit: int = Query(100, ge=1, le=1000)) -> list[AuditRowOut]:
    pipeline = require_pipeline()
    await pipeline.writer.flush()
    async with pipeline.db.session() as session:
        return [AuditRowOut(**r) for r in await repo.list_audit(session, limit)]


@router.get("/audit/verify", dependencies=[analyst_only])
async def audit_verify() -> dict[str, Any]:
    """Recompute the SHA-256 hash chain of the whole audit log."""
    pipeline = require_pipeline()
    await pipeline.writer.flush()
    async with pipeline.db.session() as session:
        result = await verify_chain(session)
    return result.as_dict()


@router.get("/bus/dlq", dependencies=[analyst_only])
async def dead_letters(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Dead-letter queue contents (in-memory bus + Redis ingress stream)."""
    pipeline = require_pipeline()
    entries = pipeline.bus.dlq_entries(limit)
    size = len(pipeline.bus.dlq)
    if pipeline.ingress is not None:
        entries += await pipeline.ingress.dlq_entries(limit)
        size += await pipeline.ingress.dlq_size()
    return {"size": size, "entries": entries[:limit]}


@router.post("/transactions", response_model=AnalyzedTransactionOut)
@limiter.limit(ingest_limit)
async def ingest(
    request: Request,
    tx: TransactionIn,
    principal: Principal = Depends(ingest_principal),
) -> AnalyzedTransactionOut:
    """Live-ingest a transaction (service key / HMAC / analyst JWT required)."""
    pipeline = require_pipeline()
    payload: dict[str, Any] = tx.model_dump(mode="json", exclude_none=True)
    payload["ts"] = tx.ts.isoformat()
    payload["ingested_by"] = principal.username
    result = await pipeline.ingest(payload)
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="İşlem analiz edilemedi",
        )
    if result.get("well_formed") is False:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "İşlem reddedildi", "issues": result.get("issues", [])},
        )
    return AnalyzedTransactionOut(**result)
