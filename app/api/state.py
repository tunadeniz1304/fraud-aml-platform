"""Shared mutable pipeline state for the dashboard API layer.

Kept in its own module so ``dashboard.py`` (app + routers) and ``admin.py`` can
both reference the live pipeline without a circular import.
"""

from __future__ import annotations

from typing import Any


class PipelineState:
    """Holds mutable references to the live pipeline components."""

    def __init__(self) -> None:
        self.monitor: Any = None
        self.analyst: Any = None
        self.action: Any = None
        self.store: Any = None
        self.vector: Any = None
        self.bus: Any = None

    @property
    def wired(self) -> bool:
        return self.monitor is not None and self.action is not None


state = PipelineState()
