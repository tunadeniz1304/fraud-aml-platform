"""Pipeline read endpoints + authenticated live ingest."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field

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
from app.security.challenges import CHALLENGES
from app.security.deps import ingest_principal, require_role, service_principal
from app.security.ratelimit import ingest_limit, limiter, principal_rate_key, step_up_limit

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
    await pipeline.settle()
    async with pipeline.db.session() as session:
        return [AuditRowOut(**r) for r in await repo.list_audit(session, limit)]


@router.get("/audit/verify", dependencies=[analyst_only])
async def audit_verify() -> dict[str, Any]:
    """Recompute the SHA-256 hash chain of the whole audit log."""
    pipeline = require_pipeline()
    await pipeline.settle()
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
@limiter.limit(ingest_limit, key_func=principal_rate_key)
async def ingest(
    request: Request,
    tx: TransactionIn,
    principal: Principal = Depends(ingest_principal),
) -> AnalyzedTransactionOut:
    """Live-ingest a transaction (service key / HMAC; analyst JWT outside prod).

    A ``STEP_UP`` decision carries a one-time ``step_up_challenge_id`` that the
    channel must present with the OTP outcome.
    """
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
    out = AnalyzedTransactionOut(**result)
    if out.decision == "STEP_UP":
        challenge = await CHALLENGES.issue(out.transaction_id, out.customer_id)
        if challenge is not None:  # None: already issued (duplicate / replayed ingest)
            out.step_up_challenge_id = challenge.challenge_id
    return out


class StepUpResultIn(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    success: bool


_INVALID_CHALLENGE = "Step-up doğrulaması geçersiz, süresi dolmuş ya da zaten kullanılmış"


@router.post("/transactions/{transaction_id}/step-up-result")
@limiter.limit(step_up_limit, key_func=principal_rate_key)
async def step_up_result(
    request: Request,
    transaction_id: str,
    body: StepUpResultIn,
    principal: Principal = Depends(service_principal),
) -> dict[str, Any]:
    """OTP / step-up challenge outcome (channel callback, service credentials only).

    The one-time challenge issued with the ``STEP_UP`` decision is consumed
    here; it must belong to this transaction. Only a verified pass adds the
    device and payee to the customer's profile, so the same legitimate device
    is not challenged forever.
    """
    pipeline = require_pipeline()
    challenge = await CHALLENGES.consume(body.challenge_id)
    if challenge is None or challenge.transaction_id != transaction_id:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=_INVALID_CHALLENGE)
    try:
        return await pipeline.step_up_result(
            transaction_id,
            success=body.success,
            actor=principal.username,
            expected_customer=challenge.customer_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="İşlem bulunamadı") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
