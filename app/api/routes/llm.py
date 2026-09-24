"""LLM status and asynchronous transaction narrative endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_pipeline
from app.api.schemas import LLMStatusOut
from app.api.state import state
from app.llm.tasks import narrate_transaction
from app.security.deps import require_role

router = APIRouter(prefix="/api/llm", tags=["llm"], dependencies=[Depends(require_role("analist"))])


@router.get("/status", response_model=LLMStatusOut)
async def llm_status() -> LLMStatusOut:
    """LLM mode/model/latency counters — the API key is never included."""
    if state.llm is None:
        raise HTTPException(status_code=503, detail="LLM servisi henüz hazır değil")
    return LLMStatusOut(**state.llm.status())


@router.post("/explain/{transaction_id}")
async def explain(transaction_id: str) -> dict[str, Any]:
    """Advisory narrative for a scored transaction (does not change the decision)."""
    pipeline = require_pipeline()
    analyzed = pipeline.results.get(transaction_id)
    if analyzed is None or analyzed.get("well_formed") is False:
        raise HTTPException(status_code=404, detail="İşlem bulunamadı")
    behaviour = None
    if pipeline.vector is not None:
        hit = await asyncio.to_thread(pipeline.vector.retrieve, analyzed)
        behaviour = hit.document if hit else None
    result = await narrate_transaction(
        pipeline.llm,
        analyzed,
        behaviour_context=behaviour,
        names=pipeline.customer_names(),
    )
    return {
        "transaction_id": transaction_id,
        **result.output.model_dump(),
        "behaviour_context": behaviour,
        **result.meta(),
    }
