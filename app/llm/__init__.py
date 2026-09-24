"""LLM analysis client for the fraud detection pipeline.

Real providers only (OpenAI / Anthropic); ``LLM_PROVIDER=none`` disables LLM
calls while keeping the rule engine and RAG vector comparison fully active.
No fake/mock completion is ever produced — when no provider is configured the
client returns ``None`` and the pipeline proceeds without a narrative analysis.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app import config

logger = logging.getLogger("fraud.llm")

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment, misc]

try:
    from anthropic import Anthropic
except Exception:  # pragma: no cover
    Anthropic = None  # type: ignore[assignment, misc]

_SUPPORTED_PROVIDERS = ("none", "openai", "anthropic")

_SYSTEM = (
    "Sen bankacılık dolandırıcılık analizinde uzmanlaşmış kıdemli bir siber "
    "güvenlik mühendisisin. Bir para transferini, verilen müşteri davranış "
    "bağlamı ve risk faktörleriyle değerlendir. SADECE geçerli bir JSON "
    "nesnesi döndür; başka metin yazma. Anahtarlar: is_fraud (boolean), "
    "fraud_type (string), explanation (string, TR), recommended_action "
    "(string: block|review|pass), confidence (float 0-1)."
)


class LLMClientError(RuntimeError):
    """Raised when an LLM provider is configured but unusable."""


class LLMClient:
    """Real-provider wrapper; structured JSON analysis of one transaction."""

    def __init__(self, provider: str | None = None) -> None:
        self.provider = (provider or config.settings.llm_provider).strip().lower()
        if self.provider not in _SUPPORTED_PROVIDERS:
            raise LLMClientError(
                f"Bilinmeyen LLM sağlayıcısı '{self.provider}'. "
                f"Desteklenen: {', '.join(_SUPPORTED_PROVIDERS)}"
            )
        self.model: str = "none"
        self._client: Any = None
        self._init_client()

    def _init_client(self) -> None:
        if self.provider == "none":
            return
        if self.provider == "openai":
            key = config.settings.openai_api_key.strip()
            if not key:
                raise LLMClientError("LLM_PROVIDER=openai ama OPENAI_API_KEY boş (.env)")
            if OpenAI is None:
                raise LLMClientError("openai paketi kurulu değil")
            self._client = OpenAI(api_key=key)
            self.model = config.settings.openai_model
            return
        key = config.settings.anthropic_api_key.strip()
        if not key:
            raise LLMClientError("LLM_PROVIDER=anthropic ama ANTHROPIC_API_KEY boş (.env)")
        if Anthropic is None:
            raise LLMClientError("anthropic paketi kurulu değil")
        self._client = Anthropic(api_key=key)
        self.model = config.settings.anthropic_model

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    # --- internals ---------------------------------------------------------
    def _completion(self, *, system: str, user: str, max_tokens: int = 700) -> str:
        if self.provider == "openai":
            resp = self._client.responses.create(  # type: ignore[union-attr]
                model=self.model,
                instructions=system,
                input=user,
                max_output_tokens=max_tokens,
            )
            return resp.output_text
        resp = self._client.messages.create(  # type: ignore[union-attr]
            model=self.model,
            system=system,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        text = text[text.find("{") : text.rfind("}") + 1] if "{" in text else text
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("LLM yanıtı bir nesne değil")
        return parsed

    def analyze(
        self,
        *,
        transaction: dict[str, Any],
        profile: dict[str, Any] | None,
        risk_factors: dict[str, float] | None,
        semantic_distance: float | None = None,
    ) -> dict[str, Any] | None:
        """Return structured LLM verdict, or ``None`` when provider is off."""
        if not self.enabled:
            return None
        context = {
            "transaction": transaction,
            "customer_profile": profile or {},
            "risk_factors": risk_factors or {},
            "semantic_distance": semantic_distance,
        }
        user = "Aşağıdaki para transferini değerlendir ve fraud analizi yap.\n" + json.dumps(
            context, ensure_ascii=False, default=str
        )
        try:
            raw = self._completion(system=_SYSTEM, user=user)
            verdict = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("[LLM] Analiz yanıtı ayrıştırılamadı: %s", exc)
            raise LLMClientError(f"LLM yanıtı geçersiz JSON: {exc}") from exc
        return verdict
