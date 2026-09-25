"""Live LLM backends.

* :class:`OpenAICompatibleBackend` — the single default client: ``AsyncOpenAI``
  against any OpenAI-compatible Chat Completions endpoint (DeepSeek V4 Flash by
  default). Handles ``response_format`` / ``tools``
  capability probing, ignores DeepSeek ``reasoning_content`` and classifies
  every failure into an :data:`~app.llm.types.ErrorKind`.
* :class:`AnthropicBackend` — optional (``LLM_PROVIDER=anthropic``); only
  imported when selected.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.llm.config import LLMSettings
from app.llm.types import ChatRequest, ChatResponse, LLMCallError, ToolCall, Usage

logger = logging.getLogger("fraud.llm")


def _classify(exc: Exception) -> LLMCallError:
    """Map SDK/transport exceptions to a stable error kind (no secrets)."""
    if isinstance(exc, LLMCallError):
        return exc
    try:
        import openai
    except ImportError:  # pragma: no cover
        return LLMCallError("unknown", type(exc).__name__)
    if isinstance(exc, openai.APITimeoutError):
        return LLMCallError("timeout", "LLM zaman aşımı")
    if isinstance(exc, openai.RateLimitError):
        return LLMCallError("rate_limit", "LLM hız sınırı (429)")
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return LLMCallError("auth", "LLM kimlik doğrulama hatası")
    if isinstance(exc, openai.BadRequestError):
        return LLMCallError("bad_request", "LLM isteği reddedildi (400)")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return LLMCallError("server_error", f"LLM sunucu hatası ({exc.status_code})")
        return LLMCallError("bad_request", f"LLM HTTP {exc.status_code}")
    if isinstance(exc, openai.APIConnectionError):
        return LLMCallError("connection", "LLM bağlantı hatası")
    import asyncio

    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return LLMCallError("timeout", "LLM zaman aşımı")
    return LLMCallError("unknown", type(exc).__name__)


def _unsupported(exc: Exception, feature: str) -> bool:
    """Heuristic: did a 400 complain about an unsupported parameter?"""
    text = str(getattr(exc, "message", "") or exc).lower()
    return feature in text or "unsupported" in text or "not support" in text


class OpenAICompatibleBackend:
    """``AsyncOpenAI`` Chat Completions client (fully async, bounded retries)."""

    name = "openai-compatible"

    def __init__(self, settings: LLMSettings, client: Any | None = None) -> None:
        self.settings = settings
        self.model = settings.model
        self.supports_json_mode = True
        self.supports_tools = True
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=settings.api_key.get_secret_value(),
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
                max_retries=settings.max_retries,
            )
        self._client = client

    def _kwargs(self, request: ChatRequest, *, stream: bool = False) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "temperature": (
                request.temperature
                if request.temperature is not None
                else self.settings.temperature
            ),
            "max_tokens": request.max_tokens or self.settings.max_tokens,
        }
        if request.tools and self.supports_tools:
            kwargs["tools"] = request.tools
        if request.json_mode and self.supports_json_mode and not request.tools:
            kwargs["response_format"] = {"type": "json_object"}
        if stream:
            kwargs["stream"] = True
        return kwargs

    async def chat(self, request: ChatRequest) -> ChatResponse:
        import openai

        kwargs = self._kwargs(request)
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            if "response_format" in kwargs and _unsupported(exc, "response_format"):
                # Endpoint does not support JSON mode: remember and retry as text.
                logger.info("[LLM] response_format desteklenmiyor — düz metin moduna geçildi")
                self.supports_json_mode = False
                kwargs.pop("response_format")
                try:
                    resp = await self._client.chat.completions.create(**kwargs)
                except Exception as inner:  # noqa: BLE001 - sınıflandırılıp yeniden fırlatılır
                    raise _classify(inner) from None
            elif "tools" in kwargs and _unsupported(exc, "tool"):
                self.supports_tools = False
                raise LLMCallError(
                    "tools_unsupported", "uç nokta tool çağrısını desteklemiyor"
                ) from None
            else:
                raise _classify(exc) from None
        except Exception as exc:  # noqa: BLE001 - sınıflandırılıp yeniden fırlatılır
            raise _classify(exc) from None

        choice = resp.choices[0].message
        # DeepSeek "thinking" models may add ``reasoning_content``: ignored on purpose.
        content = choice.content or ""
        calls: list[ToolCall] = []
        for call in getattr(choice, "tool_calls", None) or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=args))
        usage = getattr(resp, "usage", None)
        return ChatResponse(
            content=content,
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            ),
            model=getattr(resp, "model", self.model) or self.model,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        try:
            stream = await self._client.chat.completions.create(
                **self._kwargs(request, stream=True)
            )
            async with stream:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    text = getattr(delta, "content", None)  # reasoning_content ignored
                    if text:
                        yield text
        except LLMCallError:
            raise
        except Exception as exc:  # noqa: BLE001 - sınıflandırılıp yeniden fırlatılır
            raise _classify(exc) from None


class AnthropicBackend:  # pragma: no cover - optional provider, exercised manually
    """Optional Anthropic Messages API backend (``LLM_PROVIDER=anthropic``)."""

    name = "anthropic"

    def __init__(self, settings: LLMSettings) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise LLMCallError("unknown", "anthropic paketi kurulu değil") from exc
        self.settings = settings
        self.model = settings.anthropic_model
        self._client: Any = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )

    @staticmethod
    def _split(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        return system, rest

    async def chat(self, request: ChatRequest) -> ChatResponse:
        system, messages = self._split(request.messages)
        try:
            resp = await self._client.messages.create(
                model=self.model,
                system=system,
                messages=messages,
                max_tokens=request.max_tokens or self.settings.max_tokens,
                temperature=request.temperature or self.settings.temperature,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMCallError("unknown", type(exc).__name__) from None
        text = "".join(str(getattr(b, "text", "")) for b in resp.content if b.type == "text")
        return ChatResponse(
            content=text,
            usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens),
            model=self.model,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        response = await self.chat(request)
        yield response.content
