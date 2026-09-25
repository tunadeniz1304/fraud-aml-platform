"""Health, readiness and Prometheus metrics endpoints (public)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.state import state
from app.config import get_settings
from app.monitoring.metrics import REGISTRY
from app.security.auth import constant_time_equals

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
    checks: dict[str, bool] = {"pipeline": state.wired}
    pipeline = state.pipeline
    if pipeline is not None:
        checks["database"] = await pipeline.db.ping()
        if pipeline.redis is not None:
            try:
                checks["redis"] = bool(await pipeline.redis.ping())
            except Exception:  # noqa: BLE001 - readiness probe must not raise
                checks["redis"] = False
    ok = all(checks.values())
    info: dict[str, Any] = {}
    engine = getattr(getattr(pipeline, "analyst", None), "engine", None)
    if engine is not None:
        # A missing model degrades to rules-only scoring; it is reported, not fatal.
        info["model"] = engine.model.version if engine.model else "yalnız-kurallar"
        info["challenger"] = engine.challenger.version if engine.challenger else None
        info["ruleset"] = engine.ruleset.version
        info["model_loaded"] = engine.model is not None
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks, "info": info},
    )


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus scrape endpoint: public only with ``METRICS_PUBLIC=true``;
    otherwise ``Authorization: Bearer <METRICS_TOKEN>`` (404 when no token is
    configured, so the endpoint is not even discoverable)."""
    settings = get_settings()
    if not settings.metrics_public:
        expected = settings.metrics_token.get_secret_value()
        if not expected:
            raise HTTPException(status_code=404)
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not constant_time_equals(given, expected):
            raise HTTPException(status_code=401, detail="Metrik token'ı gerekli")
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
