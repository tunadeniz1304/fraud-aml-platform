"""Shared mutable pipeline state for the API layer.

Kept in its own module so routers can reference the live pipeline without a
circular import. ``attach``/``detach`` are called by the FastAPI lifespan.
"""

from __future__ import annotations

from typing import Any


class PipelineState:
    """Holds references to the live pipeline components."""

    def __init__(self) -> None:
        self.pipeline: Any = None
        self.monitor: Any = None
        self.analyst: Any = None
        self.action: Any = None
        self.store: Any = None
        self.vector: Any = None
        self.bus: Any = None
        self.llm: Any = None

    def attach(self, pipeline: Any) -> None:
        self.pipeline = pipeline
        for name in ("monitor", "analyst", "action", "store", "vector", "bus", "llm"):
            setattr(self, name, getattr(pipeline, name, None))

    def detach(self) -> None:
        self.__init__()  # type: ignore[misc]

    @property
    def wired(self) -> bool:
        return self.monitor is not None and self.action is not None


state = PipelineState()
