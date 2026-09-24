"""Provider-neutral request/response types shared by all LLM backends."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel

LLMMode = Literal["live", "demo", "fallback"]
ErrorKind = Literal[
    "timeout",
    "rate_limit",
    "server_error",
    "connection",
    "bad_request",
    "auth",
    "invalid_json",
    "schema_invalid",
    "citation_invalid",
    "tools_unsupported",
    "unknown",
]

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ToolCall:
    """A function call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class ChatRequest:
    """One chat-completion request.

    ``task`` and ``context`` are **local only**: the deterministic demo backend
    renders its answer from them; live backends send nothing but ``messages``
    (already redacted) and ``tools`` over the wire.
    """

    messages: list[dict[str, Any]]
    task: str = "generic"
    context: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] | None = None
    json_mode: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""


class LLMBackend(Protocol):
    """What every backend (live or deterministic demo) implements."""

    name: str
    model: str

    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[str]: ...


class LLMCallError(Exception):
    """A classified failure of a live LLM call (triggers demo fallback)."""

    def __init__(self, kind: ErrorKind, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind: ErrorKind = kind


@dataclass
class LLMResult(Generic[T]):
    """Validated output plus provenance for UI/metrics."""

    output: T
    llm_mode: LLMMode
    model: str
    latency_ms: float
    llm_error_kind: ErrorKind | None = None
    usage: Usage = field(default_factory=Usage)
    trace: list[dict[str, Any]] = field(default_factory=list)

    def meta(self) -> dict[str, Any]:
        return {
            "llm_mode": self.llm_mode,
            "llm_model": self.model,
            "llm_latency_ms": round(self.latency_ms, 1),
            "llm_error_kind": self.llm_error_kind,
            "llm_tokens": {
                "prompt": self.usage.prompt_tokens,
                "completion": self.usage.completion_tokens,
            },
        }
