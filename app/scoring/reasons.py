"""Reason codes (explainability, P0.5).

Every decision carries its 3–5 most influential reasons in Turkish:

* **rule reasons** — the fired rule's ``reason_template`` rendered with the
  transaction's feature values (``{amount_ratio:.1f}`` → ``8,4``);
* **model reasons** — LightGBM TreeSHAP contributions (``pred_contrib``,
  identical to ``shap.TreeExplainer``) aggregated per feature group and mapped
  to ``ML_*`` codes;
* **policy reasons** — overrides such as a sanctions hit or a blocked account.
"""

from __future__ import annotations

import math
import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.features.definitions import FEATURES, describe


class _TurkishFormatter(string.Formatter):
    """``str.format`` with a decimal comma for numbers; missing fields -> '?'."""

    def get_value(self, key: int | str, args: Any, kwargs: Any) -> Any:
        if isinstance(key, str):
            return kwargs.get(key, "?")
        return super().get_value(key, args, kwargs)  # pragma: no cover - positional unused

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, bool):
            return "evet" if value else "hayır"
        if isinstance(value, int | float):
            try:
                return format(value, format_spec).replace(".", ",")
            except ValueError:
                return str(value)
        return super().format_field(value, "")


_FORMATTER = _TurkishFormatter()


def render_template(template: str, values: Mapping[str, Any]) -> str:
    """Render a reason template; never raises (bad templates degrade to raw text)."""
    try:
        return _FORMATTER.vformat(template, (), dict(values))
    except (ValueError, IndexError, KeyError, AttributeError):
        return template


@dataclass(frozen=True)
class ReasonCode:
    code: str
    text: str
    source: str  # rule | ml | policy | signal
    weight: float
    feature: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "text": self.text,
            "source": self.source,
            "weight": round(self.weight, 4),
        }
        if self.feature:
            out["feature"] = self.feature
        return out


#: Feature group -> (ML reason code, Turkish text)
ML_GROUP_CODES: dict[str, tuple[str, str]] = {
    "tutar": ("ML_AMOUNT", "Model: tutar müşteri profiline göre olağandışı"),
    "hız": ("ML_VELOCITY", "Model: işlem sıklığı/hacmi olağandışı"),
    "alıcı": ("ML_PAYEE", "Model: alıcı ilişkisi riskli (yeni/az bilinen alıcı)"),
    "cihaz": ("ML_DEVICE_NETWORK", "Model: cihaz/ağ/konum sinyalleri riskli"),
    "zaman": ("ML_TIME", "Model: işlem zamanı müşteri için olağandışı"),
    "kanal": ("ML_CHANNEL", "Model: kanal kullanımı olağandışı"),
    "oturum": ("ML_SESSION", "Model: oturum/davranışsal biyometri sinyalleri riskli"),
    "metin": ("ML_SOCIAL_ENGINEERING", "Model: açıklama metni sosyal mühendislik içeriyor"),
    "müşteri": ("ML_CUSTOMER", "Model: müşteri kırılganlık profili riski artırıyor"),
    "harici": ("ML_EXTERNAL", "Model: harici sinyaller riski artırıyor"),
}

POLICY_CODES: dict[str, str] = {
    "SANCTIONS_HIT": "Yaptırım/PEP listesi eşleşmesi — zorunlu bekletme ve vaka",
    "ACCOUNT_BLOCKED": "Hesap zaten BLOKE — işlem skorlanmadan reddedildi",
    "UNKNOWN_CUSTOMER": "Bilinmeyen müşteri — insan incelemesine yönlendirildi",
    "RULE_FLOOR": "Kural aksiyon tabanı uygulandı",
    "ANOMALY_HIGH": "Davranış anomali skoru çok yüksek (IsolationForest/ECOD)",
}


def feature_group(name: str) -> str:
    defn = FEATURES.get(name)
    return defn.group if defn is not None else "harici"


def _fmt(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".replace(".", ",")


def ml_reasons(
    contributions: Mapping[str, float],
    features: Mapping[str, float],
    *,
    min_contribution: float = 0.05,
) -> list[ReasonCode]:
    """Aggregate positive SHAP (log-odds) contributions per feature group.

    The weight maps a log-odds push onto ``(0, 1)`` with ``1 - e^-x`` so it is
    comparable to rule scores when ranking.
    """
    groups: dict[str, list[tuple[str, float]]] = {}
    for name, value in contributions.items():
        if value > 0:
            groups.setdefault(feature_group(name), []).append((name, value))
    out: list[ReasonCode] = []
    for group, items in groups.items():
        total = sum(v for _, v in items)
        if total < min_contribution:
            continue
        top_name, _ = max(items, key=lambda p: p[1])
        code, text = ML_GROUP_CODES.get(group, ML_GROUP_CODES["harici"])
        top_value = features.get(top_name)
        detail = f" (en etkili: {describe(top_name)} = {_fmt(top_value)})" if top_value else ""
        out.append(ReasonCode(code, text + detail, "ml", 1.0 - math.exp(-total), top_name))
    out.sort(key=lambda r: -r.weight)
    return out


def policy_reason(code: str, weight: float = 1.0, text: str | None = None) -> ReasonCode:
    return ReasonCode(code, text or POLICY_CODES.get(code, code), "policy", weight)


def top_reasons(
    *groups: Iterable[ReasonCode], k: int = 5, min_weight: float = 0.0
) -> list[ReasonCode]:
    """Policy reasons first, then everything else by weight; one entry per code."""
    merged: dict[str, ReasonCode] = {}
    for group in groups:
        for reason in group:
            current = merged.get(reason.code)
            if current is None or reason.weight > current.weight:
                merged[reason.code] = reason
    ordered = sorted(
        (r for r in merged.values() if r.source == "policy" or r.weight >= min_weight),
        key=lambda r: (r.source != "policy", -r.weight),
    )
    return ordered[:k]
