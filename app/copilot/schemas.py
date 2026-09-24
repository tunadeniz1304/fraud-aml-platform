"""Copilot output schemas (validated with pydantic) and the citation validator.

Every claim the copilot makes must cite evidence that really exists in the
case dossier (transaction ids, alert ids, reason codes, case events …). The
:func:`citation_validator` rejects an answer that cites anything else — a
hallucinated id triggers one repair attempt and then the deterministic
fallback (§3.3).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field


class SummaryBullet(BaseModel):
    text: str = Field(min_length=5, max_length=600)
    citations: list[str] = Field(min_length=1, max_length=8)


class CaseSummary(BaseModel):
    bullets: list[SummaryBullet] = Field(min_length=3, max_length=5)
    risk_assessment: str = Field(min_length=5, max_length=600)


class Recommendation(BaseModel):
    decision: Literal["FRAUD", "TEMIZ", "INCELEMEYE_DEVAM"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=10, max_length=1500)
    citations: list[str] = Field(min_length=1, max_length=10)
    next_steps: list[str] = Field(default_factory=list, max_length=6)


class SibTransaction(BaseModel):
    transaction_id: str
    tarih: str
    tutar: float
    para_birimi: str = "TRY"
    tutar_try: float
    alici: str = ""
    aciklama: str = ""


class SibSubject(BaseModel):
    musteri_no: str
    ad_soyad: str
    hesap: str = ""
    dogum_yili: int | None = None


class SibDraft(BaseModel):
    """MASAK Şüpheli İşlem Bildirimi taslağı (5549 s. Kanun, md. 4)."""

    bildirim_turu: Literal["ŞİB"] = "ŞİB"
    supheli_islem_tipi: str = Field(min_length=3, max_length=200)
    supheli: SibSubject
    kim: str = Field(min_length=5, max_length=1000)
    ne: str = Field(min_length=5, max_length=1500)
    ne_zaman: str = Field(min_length=5, max_length=500)
    nerede: str = Field(min_length=3, max_length=500)
    neden: str = Field(min_length=10, max_length=3000)
    islemler: list[SibTransaction] = Field(min_length=1)
    toplam_tutar_try: float = Field(ge=0)
    supheye_ilk_ulasilma: str
    bildirim_son_tarihi: str
    tipping_off_uyarisi: str = Field(min_length=20)
    citations: list[str] = Field(min_length=1)
    hazirlayan: str = "Anil3 Copilot (taslak — analist onayı gerekir)"


class TriageLabel(BaseModel):
    """Async APP text classification (limited-weight case enrichment only)."""

    label: Literal["guvenli_hesap", "yatirim", "sahte_yetkili", "romantik", "diger", "yok"]
    confidence: float = Field(ge=0.0, le=1.0)


def _collect(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "citations" and isinstance(value, list):
                yield from (str(v) for v in value)
            else:
                yield from _collect(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _collect(item)


def citation_validator(allowed: Iterable[str]) -> Callable[[Any], list[str]]:
    """Reject outputs citing evidence ids that are not in the dossier."""
    known = {str(a) for a in allowed}

    def check(obj: Any) -> list[str]:
        if isinstance(obj, BaseModel):
            obj = obj.model_dump()
        unknown = sorted({c for c in _collect(obj) if c not in known})
        return [f"vakada olmayan kanıt kimliği: {c}" for c in unknown]

    return check
