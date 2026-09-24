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

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin
from app.api.frontend import STATIC_DIR
from app.api.frontend import render as render_frontend
from app.api.routes import auth, cases, health, llm, pipeline, rules
from app.api.security import install as install_security
from app.api.state import state
from app.config import get_settings
from app.monitoring.logging import configure_logging
from app.monitoring.tracing import setup_tracing
from app.security.auth import UserDirectory

logger = logging.getLogger("fraud.dashboard")

PipelineFactory = Callable[[], Awaitable[Any]]


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
    app.state.users = UserDirectory.with_demo_users()
    install_security(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(pipeline.router)
    app.include_router(llm.router)
    app.include_router(rules.router)
    app.include_router(cases.router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return render_frontend()

    return app


app = create_app()
