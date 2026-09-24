"""Deterministic demo LLM (§3.3 "Demo modu").

Implements the same :class:`~app.llm.types.LLMBackend` interface as the live
client but renders Turkish, template-based, *reasoned* answers from the
structured ``ChatRequest.context`` (scores, factors, reason codes, case data).
The same input always yields the same output, so every UI flow — narrative,
copilot tool loop, ŞİB draft, analyst chat — works without an API key.

Feature modules register their own renderers with :func:`register_template`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.llm.types import ChatRequest, ChatResponse, ToolCall, Usage

TemplateFn = Callable[[dict[str, Any]], dict[str, Any] | str]
_TEMPLATES: dict[str, TemplateFn] = {}


def register_template(task: str) -> Callable[[TemplateFn], TemplateFn]:
    """Decorator: register the deterministic renderer for ``task``."""

    def deco(fn: TemplateFn) -> TemplateFn:
        _TEMPLATES[task] = fn
        return fn

    return deco


def fmt_try(value: float | int | None) -> str:
    """Turkish money format: 1234567.8 → '1.234.567,80 TL'."""
    amount = float(value or 0)
    whole, frac = f"{amount:,.2f}".split(".")
    return f"{whole.replace(',', '.')},{frac} TL"


# --- built-in templates -----------------------------------------------------
@register_template("smoke")
def _smoke(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "message": "Demo LLM çalışıyor"}


@register_template("txn_narrative")
def _txn_narrative(ctx: dict[str, Any]) -> dict[str, Any]:
    tx = ctx.get("transaction", {})
    risk = float(ctx.get("risk_score", 0.0))
    reasons: list[str] = list(ctx.get("reasons", []))
    behaviour = ctx.get("behaviour_context") or ""
    level = "yüksek" if risk >= 0.75 else "orta" if risk >= 0.5 else "düşük"
    action = "block" if risk >= 0.75 else "review" if risk >= 0.5 else "pass"
    parts = [
        f"{tx.get('transaction_id', '?')} numaralı {fmt_try(ctx.get('amount_try'))} "
        f"tutarındaki transfer {level} risk ({risk:.2f}) taşıyor."
    ]
    if reasons:
        parts.append("Başlıca nedenler: " + "; ".join(reasons[:4]) + ".")
    else:
        parts.append("Kayda değer bir risk sinyali tespit edilmedi.")
    if behaviour:
        parts.append("Müşterinin olağan davranışıyla karşılaştırıldı: " + behaviour[:220])
    return {
        "summary": " ".join(parts),
        "fraud_type": ctx.get("fraud_type") or ("şüpheli" if risk >= 0.5 else "yok"),
        "recommended_action": action,
        "confidence": round(min(0.95, 0.55 + abs(risk - 0.5)), 2),
    }


@register_template("chat")
def _chat(ctx: dict[str, Any]) -> str:
    question = str(ctx.get("question", "")).strip()
    facts: list[str] = list(ctx.get("facts", []))
    head = f"Soru: “{question}”. " if question else ""
    if not facts:
        return head + "Bu vaka için kayıtlı kanıt bulunamadı; lütfen vaka verisini kontrol edin."
    return head + "Kanıtlara göre: " + " ".join(f"({i + 1}) {f}" for i, f in enumerate(facts[:5]))


class DeterministicLLM:
    """Template-driven, reproducible stand-in for a live model."""

    name = "demo"
    model = "deterministic-demo"

    def _render(self, request: ChatRequest) -> str:
        fn = _TEMPLATES.get(request.task)
        if fn is None:
            payload: dict[str, Any] | str = {
                "message": f"Demo modu: '{request.task}' görevi için şablon yok."
            }
        else:
            payload = fn(request.context)
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if request.tools:
            plan: list[dict[str, Any]] = list(request.context.get("tool_plan", []))
            done = sum(1 for m in request.messages if m.get("role") == "tool")
            if done < len(plan):
                step = plan[done]
                return ChatResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"demo-call-{done + 1}",
                            name=str(step["name"]),
                            arguments=dict(step.get("arguments", {})),
                        )
                    ],
                    model=self.model,
                )
        content = self._render(request)
        return ChatResponse(
            content=content,
            usage=Usage(prompt_tokens=0, completion_tokens=0),
            model=self.model,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        text = self._render(request)
        words = text.split(" ")
        for i, word in enumerate(words):
            yield word + (" " if i < len(words) - 1 else "")
