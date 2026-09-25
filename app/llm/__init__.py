"""LLM integration (§3 LLM contract).

Plug-and-play: with a key in ``.env`` the platform talks to DeepSeek V4 Flash
(or any OpenAI-compatible endpoint); without one it runs the
deterministic Turkish demo backend. The LLM never sits on the synchronous
scoring path and never decides — it summarises, investigates and drafts.
"""

from __future__ import annotations

from app.llm.config import LLMConfigError, LLMSettings, mask_secret
from app.llm.service import LLMService
from app.llm.types import ChatRequest, ChatResponse, LLMCallError, LLMResult

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "LLMCallError",
    "LLMConfigError",
    "LLMResult",
    "LLMService",
    "LLMSettings",
    "mask_secret",
]
