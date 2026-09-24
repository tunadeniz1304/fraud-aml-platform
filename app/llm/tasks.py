"""Concrete LLM tasks built on :class:`~app.llm.service.LLMService`.

``narrate_transaction`` produces the asynchronous analyst narrative for one
scored transfer. It is **advisory only**: nothing it returns changes the risk
score or the decision (§6). Retrieved behaviour context (Chroma RAG) is passed
to the model as evidence — the retrieval result actually reaches the prompt.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.llm.service import LLMService
from app.llm.types import LLMResult

NARRATIVE_SYSTEM = (
    "Sen bir bankanın fraud operasyon ekibinde kıdemli analistsin. Verilen skorlanmış "
    "para transferini, risk nedenlerini ve müşterinin geçmiş davranış özetini kullanarak "
    "Türkçe, kısa ve kanıta dayalı bir değerlendirme yaz. Karar verme: yalnızca öner. "
    "Veride olmayan bilgi uydurma."
)


class TransactionNarrative(BaseModel):
    summary: str = Field(min_length=10, max_length=2000)
    fraud_type: str = Field(default="yok", max_length=80)
    recommended_action: Literal["block", "review", "pass"]
    confidence: float = Field(ge=0.0, le=1.0)


async def narrate_transaction(
    llm: LLMService,
    analyzed: dict[str, Any],
    *,
    behaviour_context: str | None = None,
    names: list[str] | None = None,
) -> LLMResult[TransactionNarrative]:
    """Async narrative for an analyzed transaction (never on the scoring path)."""
    reasons = list(analyzed.get("risk_explanation") or [])
    reasons += [r.get("text", "") for r in analyzed.get("reason_codes") or [] if r.get("text")]
    payload: dict[str, Any] = {
        "transaction": {
            k: analyzed.get(k)
            for k in (
                "transaction_id",
                "customer_id",
                "amount",
                "currency",
                "channel",
                "location",
                "country",
                "purpose",
                "beneficiary_id",
            )
        },
        "amount_try": analyzed.get("amount_try"),
        "risk_score": analyzed.get("risk_score"),
        "decision": analyzed.get("decision"),
        "reasons": reasons,
        "sanctions_hit": analyzed.get("sanctions_hit", False),
        "behaviour_context": behaviour_context or "",
    }
    return await llm.generate(
        task="txn_narrative",
        system=NARRATIVE_SYSTEM,
        payload=payload,
        schema=TransactionNarrative,
        instruction=(
            "Bu transferi değerlendir: özet, olası fraud tipi, önerilen aksiyon "
            "(block|review|pass) ve 0-1 güven."
        ),
        names=names or [],
    )
