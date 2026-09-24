"""Consortium API (P2.3): shared hashed deny-list stats and (simulated) partner reports."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_pipeline
from app.security.deps import require_role

router = APIRouter(prefix="/api/consortium", tags=["consortium"])


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    bank: str = Field(min_length=2, max_length=32, pattern=r"^[A-Z0-9_]+$")
    kind: Literal["iban", "device"]
    value: str = Field(min_length=3, max_length=64)


def _hub() -> Any:
    hub = require_pipeline().engine.consortium
    if hub is None:
        raise HTTPException(status_code=404, detail="Konsorsiyum devre dışı")
    return hub


@router.get("/stats", dependencies=[Depends(require_role("analist"))])
async def stats() -> dict[str, Any]:
    return _hub().stats()


@router.post("/report", dependencies=[Depends(require_role("admin"))])
async def report(body: ReportIn) -> dict[str, str]:
    """Simulate a partner bank sharing a confirmed-fraud identifier (hashed on arrival)."""
    return {"digest": _hub().report(body.bank, body.kind, body.value)}
