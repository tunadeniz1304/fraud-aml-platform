"""Shared API dependencies."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.api.state import state


def require_pipeline() -> Any:
    """Return the live pipeline or 503 while it is not wired yet."""
    if not state.wired or state.pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline henüz bağlanmadı")
    return state.pipeline
