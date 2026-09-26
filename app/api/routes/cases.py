"""Analyst workbench API (P0.6): cases, alerts, evidence, decisions, ŞİB, approvals.

Roles: every read and routine action needs ``analist``; assigning a case to
someone else, approving/rejecting maker-checker requests needs
``kidemli_analist`` (and a *different* user than the requester).
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_pipeline
from app.api.downloads import content_disposition
from app.cases.service import (
    STATUSES,
    ApprovalUnavailableError,
    CaseError,
    CaseNotFoundError,
    CaseService,
    MakerCheckerError,
)
from app.security.auth import Principal
from app.security.deps import forbid_break_glass_maker, require_role

router = APIRouter(prefix="/api", tags=["cases"])
analyst = require_role("analist")
senior = require_role("kidemli_analist")

T = TypeVar("T")


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    assignee: str | None = Field(default=None, max_length=64)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["YENI", "INCELENIYOR", "BEKLEMEDE", "KAPANDI_FRAUD", "KAPANDI_TEMIZ"]
    note: str = Field(default="", max_length=2000)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=5000)


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[\w.\- ]+$")
    content_type: str = Field(default="application/octet-stream", max_length=100)
    content_b64: str = Field(min_length=1, max_length=1_500_000)
    note: str = Field(default="", max_length=2000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["FRAUD", "TEMIZ"]
    note: str = Field(default="", max_length=5000)


class SibDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: dict[str, Any]


class ApprovalNoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=2000)


class UnblockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_status: Literal["AKTIF", "INCELENIYOR"] = "AKTIF"
    note: str = Field(default="", max_length=2000)


def cases_service() -> CaseService:
    pipeline = require_pipeline()
    service: CaseService | None = getattr(pipeline, "cases", None)
    if service is None:  # pragma: no cover - always wired by build_pipeline
        raise HTTPException(status_code=503, detail="Vaka servisi hazır değil")
    return service


async def settled_cases() -> CaseService:
    """Case service after the write-behind queue caught up (read your writes:
    a decision returned by POST /api/transactions has its case/alert)."""
    await require_pipeline().settle()
    return cases_service()


async def _call(fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    try:
        return await fn(*args, **kwargs)
    except CaseNotFoundError:
        raise HTTPException(status_code=404, detail="Kayıt bulunamadı") from None
    except MakerCheckerError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except CaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ApprovalUnavailableError as exc:  # A5: nothing applied, retry later
        raise HTTPException(status_code=503, detail=str(exc)) from None


# --- cases ----------------------------------------------------------------------------
@router.get("/cases", dependencies=[Depends(analyst)])
async def list_cases(
    status: str | None = Query(None, description="YENI…, veya OPEN"),
    assigned_to: str | None = None,
    customer_id: str | None = None,
    case_type: str | None = None,
    order: Literal["priority", "sla", "masak", "recent"] = "priority",
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    if status and status != "OPEN" and status not in STATUSES:
        raise HTTPException(status_code=422, detail="Geçersiz vaka durumu")
    return await (await settled_cases()).list_cases(
        status=status,
        assigned_to=assigned_to,
        customer_id=customer_id,
        case_type=case_type,
        order=order,
        limit=limit,
    )


@router.get("/cases/page", dependencies=[Depends(analyst)])
async def page_cases(
    status: str | None = Query(None, description="YENI…, veya OPEN"),
    assigned_to: str | None = None,
    customer_id: str | None = None,
    case_type: str | None = None,
    order: Literal["priority", "sla", "masak", "recent"] = "priority",
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Server-side paginated + filtered case queue (``total`` for the pager)."""
    if status and status != "OPEN" and status not in STATUSES:
        raise HTTPException(status_code=422, detail="Geçersiz vaka durumu")
    service = await settled_cases()
    filters = {
        "status": status,
        "assigned_to": assigned_to,
        "customer_id": customer_id,
        "case_type": case_type,
    }
    items = await service.list_cases(**filters, order=order, limit=limit, offset=offset)
    return {
        "items": items,
        "total": await service.count_cases(**filters),
        "limit": limit,
        "offset": offset,
    }


@router.get("/cases/stats", dependencies=[Depends(analyst)])
async def case_stats() -> dict[str, Any]:
    service = await settled_cases()
    return {"by_status": await service.counts(), "sla": await service.sla_scan()}


@router.get("/cases/{case_id}", dependencies=[Depends(analyst)])
async def get_case(case_id: int) -> dict[str, Any]:
    return await _call((await settled_cases()).get_case, case_id)


@router.post("/cases/{case_id}/assign")
async def assign(
    case_id: int, body: AssignIn, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    assignee = body.assignee or principal.username
    if assignee != principal.username and not principal.has_role("kidemli_analist"):
        raise HTTPException(status_code=403, detail="Başkasına atama için kıdemli analist gerekli")
    return await _call(
        cases_service().assign, case_id, assignee, principal.username, role=principal.role
    )


@router.post("/cases/{case_id}/status")
async def set_status(
    case_id: int, body: StatusIn, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    return await _call(
        cases_service().set_status,
        case_id,
        body.status,
        principal.username,
        body.note,
        role=principal.role,
    )


@router.post("/cases/{case_id}/notes", status_code=201)
async def add_note(
    case_id: int, body: NoteIn, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    return await _call(cases_service().add_note, case_id, body.text, principal.username)


@router.post("/cases/{case_id}/evidence", status_code=201)
async def add_evidence(
    case_id: int, body: EvidenceIn, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    return await _call(
        cases_service().add_evidence,
        case_id,
        filename=body.filename,
        content_type=body.content_type,
        content_b64=body.content_b64,
        note=body.note,
        actor=principal.username,
    )


@router.get("/cases/{case_id}/evidence/{evidence_id}", dependencies=[Depends(analyst)])
async def download_evidence(case_id: int, evidence_id: int) -> Response:
    payload = await _call(cases_service().evidence, case_id, evidence_id)
    return Response(
        content=base64.b64decode(payload.get("content_b64") or ""),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(
                str(payload.get("filename") or ""), fallback="kanit"
            ),
            "X-Content-SHA256": str(payload.get("sha256", "")),
        },
    )


@router.post("/cases/{case_id}/decision")
async def decide(
    case_id: int, body: DecisionIn, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    return await _call(
        cases_service().decide,
        case_id,
        body.outcome,
        principal.username,
        body.note,
        role=principal.role,
    )


@router.put("/cases/{case_id}/sib")
async def save_sib(
    case_id: int, body: SibDraftIn, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    return await _call(cases_service().save_sib_draft, case_id, body.draft, principal.username)


@router.post("/cases/{case_id}/sib/submit", status_code=202)
async def submit_sib(
    case_id: int, body: ApprovalNoteIn | None = None, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    note = body.note if body else ""
    forbid_break_glass_maker(principal)
    return await _call(
        cases_service().request_approval, "SIB", str(case_id), {}, principal.username, note
    )


# --- alerts / accounts ------------------------------------------------------------------
@router.get("/alerts", dependencies=[Depends(analyst)])
async def alerts(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    return await (await settled_cases()).list_alerts(limit)


@router.post("/accounts/{customer_id}/unblock-request", status_code=202)
async def unblock_request(
    customer_id: str, body: UnblockIn, principal: Principal = Depends(analyst)
) -> dict[str, Any]:
    pipeline = require_pipeline()
    status = pipeline.accounts.get_status(customer_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Müşteri bulunamadı")
    if status != "BLOKE":
        raise HTTPException(status_code=409, detail="Hesap BLOKE değil")
    forbid_break_glass_maker(principal)
    return await _call(
        cases_service().request_approval,
        "UNBLOCK",
        customer_id,
        {"to_status": body.to_status, "from_status": status},
        principal.username,
        body.note,
    )


# --- approvals ------------------------------------------------------------------------------
@router.get("/approvals", dependencies=[Depends(analyst)])
async def approvals(
    status: Literal["BEKLIYOR", "ONAYLANDI", "REDDEDILDI", "ALL"] = "BEKLIYOR",
) -> list[dict[str, Any]]:
    return await cases_service().list_approvals(None if status == "ALL" else status)


@router.post("/approvals/{approval_id}/approve")
async def approve(
    approval_id: int, body: ApprovalNoteIn | None = None, principal: Principal = Depends(senior)
) -> dict[str, Any]:
    return await _call(
        cases_service().decide_approval,
        approval_id,
        approve=True,
        actor=principal.username,
        note=body.note if body else "",
        role=principal.role,
        via=principal.via,
    )


@router.post("/approvals/{approval_id}/reject")
async def reject(
    approval_id: int, body: ApprovalNoteIn | None = None, principal: Principal = Depends(senior)
) -> dict[str, Any]:
    return await _call(
        cases_service().decide_approval,
        approval_id,
        approve=False,
        actor=principal.username,
        note=body.note if body else "",
        role=principal.role,
        via=principal.via,
    )
