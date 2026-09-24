"""Health, readiness and Prometheus metrics endpoints (public)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.state import state
from app.monitoring.metrics import REGISTRY

router = APIRouter(tags=["system"])


@router.get("/api/health")
async def health() -> dict[str, Any]:
    llm_mode = state.llm.mode if state.llm is not None else None
    return {"status": "ok", "pipeline": state.wired, "llm_mode": llm_mode}


@router.get("/api/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/api/health/ready")
async def ready() -> JSONResponse:
    checks = {"pipeline": state.wired}
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
