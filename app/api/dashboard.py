"""FastAPI application factory.

The whole detection pipeline is built inside the FastAPI ``lifespan`` (fixes
bug #1: ``uvicorn server:app`` — e.g. the Docker ``CMD`` — used to serve 503 on
every ``/api/*`` route because the pipeline was only built under
``__main__``). In ``stream`` mode the simulator is a background task started
by the lifespan (fixes bug #2).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin
from app.api.frontend import STATIC_DIR
from app.api.frontend import render as render_frontend
from app.api.routes import (
    auth,
    cases,
    consortium,
    copilot,
    health,
    live,
    llm,
    models,
    network,
    pipeline,
    rules,
)
from app.api.security import install as install_security
from app.api.state import state
from app.config import get_settings
from app.idempotency import IdempotencyConflict, IngestInProgress
from app.monitoring.logging import configure_logging
from app.monitoring.tracing import setup_tracing
from app.security.auth import UserDirectory

logger = logging.getLogger("fraud.dashboard")

PipelineFactory = Callable[[], Awaitable[Any]]


def install_idempotency_errors(app: FastAPI) -> None:
    """M1: idempotency outcomes of ``POST /api/transactions`` as HTTP 409."""

    @app.exception_handler(IdempotencyConflict)
    async def _conflict(request: Request, exc: IdempotencyConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": "IDEMPOTENCY_CONFLICT",
                "transaction_id": exc.tx_id,
            },
        )

    @app.exception_handler(IngestInProgress)
    async def _processing(request: Request, exc: IngestInProgress) -> JSONResponse:
        retry_after = max(1, round(exc.retry_after_s))
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": "PROCESSING",
                "transaction_id": exc.tx_id,
                "retry_after_s": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )


def create_app(pipeline_factory: PipelineFactory | None = None) -> FastAPI:
    """Build the FastAPI app. ``pipeline_factory`` lets tests inject a pipeline."""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings)
        setup_tracing("anil3-api")
        factory = app.state.pipeline_factory
        if factory is None:
            from app.pipeline import build_pipeline

            built = await build_pipeline(settings)
        else:
            built = await factory()
        state.attach(built)
        await built.start()
        try:
            yield
        finally:
            await built.stop()
            state.detach()

    app = FastAPI(
        title="Anil3 — Fraud & AML Platformu",
        version="2.0.0",
        description="Gerçek zamanlı hibrit fraud skorlama, vaka yönetimi ve LLM copilot API'si.",
        lifespan=lifespan,
        docs_url="/docs" if settings.api_docs else None,
        redoc_url=None,
    )
    app.state.pipeline_factory = pipeline_factory
    app.state.users = UserDirectory.from_settings(settings)
    install_security(app)
    install_idempotency_errors(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(auth.stream_router)
    app.include_router(pipeline.router)
    app.include_router(llm.router)
    app.include_router(rules.router)
    app.include_router(cases.router)
    app.include_router(network.router)
    app.include_router(copilot.router)
    app.include_router(models.router)
    app.include_router(live.router)
    app.include_router(consortium.router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/legacy", response_class=HTMLResponse, include_in_schema=False)
    async def legacy() -> str:
        return render_frontend()

    dist = settings.resolved_frontend_dist
    if (dist / "index.html").is_file():
        # React SPA (built by the Docker multi-stage build / `npm run build`)
        app.mount("/app", StaticFiles(directory=dist, html=True), name="spa")

        @app.get("/", include_in_schema=False)
        async def spa() -> FileResponse:
            return FileResponse(dist / "index.html")

    else:

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def index() -> str:
            return render_frontend()

    return app


app = create_app()
