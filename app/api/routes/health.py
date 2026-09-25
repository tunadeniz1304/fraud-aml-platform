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
from app.security.deps import principal_from_token

router = APIRouter(tags=["system"])


@router.get("/api/health")
async def health() -> dict[str, Any]:
    llm_mode = state.llm.mode if state.llm is not None else None
    return {"status": "ok", "pipeline": state.wired, "llm_mode": llm_mode}


@router.get("/api/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


def _bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _authenticated(request: Request) -> bool:
    token = _bearer_token(request)
    if not token:
        return False
    try:
        principal_from_token(token)
    except HTTPException:
        return False
    return True


@router.get("/api/health/ready")
async def ready(request: Request) -> JSONResponse:
    checks: dict[str, bool] = {"pipeline": state.wired}
    pipeline = state.pipeline
    if pipeline is not None:
        checks["database"] = await pipeline.db.ping()
        # a batch stuck behind a database outage stalls acks and ingestion (A2)
        checks["writer"] = not pipeline.writer.stuck
        if pipeline.redis is not None:
            try:
                checks["redis"] = bool(await pipeline.redis.ping())
            except Exception:  # noqa: BLE001 - readiness probe must not raise
                checks["redis"] = False
    ok = all(checks.values())
    info: dict[str, Any] = {}
    engine = getattr(getattr(pipeline, "analyst", None), "engine", None)
    # Model/ruleset versions help an attacker fingerprint the deployment: only
    # an authenticated caller sees them; probes get the checks alone.
    if engine is not None and _authenticated(request):
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
        given = _bearer_token(request)
        if not constant_time_equals(given, expected):
            raise HTTPException(status_code=401, detail="Metrik token'ı gerekli")
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
