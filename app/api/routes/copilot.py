"""Copilot API (P1.5): case summary, recommendation, ŞİB draft/export, analyst chat (SSE).

Everything here is advisory: nothing changes a score, a decision or an
account. ŞİB submission still goes through the maker-checker approval.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import require_pipeline
from app.api.downloads import content_disposition
from app.cases.service import CaseError, CaseNotFoundError
from app.copilot.agent import CopilotAgent
from app.copilot.sib_export import render_sib_pdf
from app.security.auth import Principal
from app.security.deps import require_role
from app.security.ratelimit import copilot_limit, limiter, principal_rate_key

router = APIRouter(prefix="/api/cases", tags=["copilot"])
analyst = require_role("analist")


def _agent() -> CopilotAgent:
    agent: CopilotAgent | None = getattr(require_pipeline(), "copilot", None)
    if agent is None:  # pragma: no cover - always wired
        raise HTTPException(status_code=503, detail="Copilot hazır değil")
    return agent


def _response(result: Any, inv: Any, key: str) -> dict[str, Any]:
    return {
        key: result.output.model_dump(mode="json"),
        **result.meta(),
        "tool_loop": {"mode": inv.mode, "protocol": inv.protocol, "error_kind": inv.error_kind},
        "trace": inv.trace,
        "evidence_ids": sorted(inv.evidence_ids),
    }


@router.post("/{case_id}/copilot/summary")
@limiter.limit(copilot_limit, key_func=principal_rate_key)
async def summary(
    request: Request, case_id: int, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    try:
        result, inv = await _agent().summarize(case_id)
    except CaseNotFoundError:
        raise HTTPException(status_code=404, detail="Vaka bulunamadı") from None
    await require_pipeline().cases.save_summary(
        case_id, result.output.model_dump(mode="json"), principal.username
    )
    return _response(result, inv, "summary")


@router.post("/{case_id}/copilot/recommendation", dependencies=[Depends(analyst)])
@limiter.limit(copilot_limit, key_func=principal_rate_key)
async def recommendation(request: Request, case_id: int) -> dict[str, Any]:
    try:
        result, inv = await _agent().recommend(case_id)
    except CaseNotFoundError:
        raise HTTPException(status_code=404, detail="Vaka bulunamadı") from None
    body = _response(result, inv, "recommendation")
    body["notice"] = "Öneri niteliğindedir; karar ve aksiyon analist onayı gerektirir."
    return body


@router.post("/{case_id}/copilot/sib")
@limiter.limit(copilot_limit, key_func=principal_rate_key)
async def sib(
    request: Request, case_id: int, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    try:
        result, inv = await _agent().sib_draft(case_id)
        await require_pipeline().cases.save_sib_draft(
            case_id, result.output.model_dump(mode="json"), principal.username
        )
    except CaseNotFoundError:
        raise HTTPException(status_code=404, detail="Vaka bulunamadı") from None
    except CaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _response(result, inv, "sib_draft")


async def _stored_draft(case_id: int) -> dict[str, Any]:
    try:
        case = await require_pipeline().cases.get_case(case_id)
    except CaseNotFoundError:
        raise HTTPException(status_code=404, detail="Vaka bulunamadı") from None
    if not case.get("sib_draft"):
        raise HTTPException(status_code=404, detail="ŞİB taslağı yok")
    return dict(case["sib_draft"]) | {"_case": case_id, "_status": case.get("sib_status")}


@router.get("/{case_id}/sib.json", dependencies=[Depends(analyst)])
async def sib_json(case_id: int) -> Response:
    draft = await _stored_draft(case_id)
    return Response(
        json.dumps(draft, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": content_disposition(f"SIB-vaka-{case_id}.json")},
    )


@router.get("/{case_id}/sib.pdf", dependencies=[Depends(analyst)])
async def sib_pdf(case_id: int) -> Response:
    draft = await _stored_draft(case_id)
    return Response(
        render_sib_pdf(draft),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(f"SIB-vaka-{case_id}.pdf")},
    )


class ChatIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


@router.post("/{case_id}/chat", dependencies=[Depends(analyst)])
@limiter.limit(copilot_limit, key_func=principal_rate_key)
async def chat(request: Request, case_id: int, body: ChatIn) -> StreamingResponse:
    """Analyst chat about a case, streamed as Server-Sent Events.

    POST with the question in the JSON body (it may contain customer details;
    a query string would end up in access logs and browser history)."""
    q = body.question
    agent = _agent()
    try:
        await require_pipeline().cases.get_case(case_id)
    except CaseNotFoundError:
        raise HTTPException(status_code=404, detail="Vaka bulunamadı") from None

    async def events() -> AsyncIterator[str]:
        async for event in agent.chat(case_id, q):
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
