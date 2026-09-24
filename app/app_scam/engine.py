"""APP (authorised push payment) scam engine (P1.3).

In an APP scam the *real* customer, on their own device, is manipulated into
sending money — so device/location anomalies are usually absent. The engine
combines the social-engineering footprint instead:

new payee · first unusually large transfer · young beneficiary account ·
CoP name mismatch · beneficiary already in a fraud network (graph) ·
urgency / "güvenli hesap" / investment wording · active phone call or
remote-access tool during the session · long session · elderly/vulnerable
customer.

Output: an ``app`` score (policy signal), a **dynamic warning** tailored to the
detected pattern, and whether the customer must confirm "Bu kişiyi tanıyor
musunuz?". The policy turns a strong APP pattern into a *HOLD with cooling-off*
instead of a hard block (the customer is the victim, not the attacker).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.app_scam.cop import CLOSE, NO_MATCH, UNKNOWN, CoPResult, PayeeRegistry
from app.config import get_settings
from app.core.signals import purpose_keyword_hits
from app.features.extractor import Extraction
from app.scoring.reasons import ReasonCode
from app.scoring.rules import noisy_or
from app.scoring.signals import SignalResult

WARNINGS = {
    "authority": (
        "Polis, savcılık, MASAK veya bankanız sizden ASLA 'güvenli hesaba' para aktarmanızı "
        "istemez. Bu bir dolandırıcılık girişimi olabilir."
    ),
    "investment": (
        "Garantili veya çok yüksek getiri vaat eden yatırım/kripto teklifleri dolandırıcılıkta "
        "sık kullanılır. Parayı göndermeden önce kurumu resmi kanallardan doğrulayın."
    ),
    "call": (
        "Şu anda telefonda biri size bu işlemi yaptırıyorsa görüşmeyi hemen sonlandırın ve "
        "bankanızı kartınızın arkasındaki numaradan arayın."
    ),
    "remote": (
        "Ekranınızı paylaştığınız / uzaktan erişim veren uygulamayı kapatın. Bankanız asla "
        "uzaktan bağlanmanızı istemez."
    ),
    "cop": (
        "Girdiğiniz alıcı adı, IBAN sahibinin kayıtlı adıyla eşleşmiyor. Alıcıyı başka bir "
        "kanaldan doğrulamadan işlem yapmayın."
    ),
    "default": (
        "Bu kişiyi tanıyor musunuz? Tanımadığınız veya yalnızca internetten/telefondan "
        "tanıdığınız kişilere para göndermeyin."
    ),
}
_AUTHORITY = ("polis", "savci", "savcı", "güvenli hesap", "guvenli hesap", "güvenli hesab", "masak")
_INVESTMENT = ("yatırım", "yatirim", "kripto", "crypto", "getiri", "garantili", "investment")


class APPScamSignal:
    name = "app"

    def __init__(self, registry: PayeeRegistry | None = None) -> None:
        self.registry = registry or PayeeRegistry()

    def _cop(self, tx: dict[str, Any]) -> CoPResult:
        return self.registry.confirm(
            str(tx.get("beneficiary_iban") or tx.get("beneficiary_id") or ""),
            str(tx.get("beneficiary_name") or ""),
        )

    def _payee_age_days(self, tx: dict[str, Any], extraction: Extraction) -> float | None:
        record = self.registry.lookup(str(tx.get("beneficiary_iban") or ""))
        if record is None or record.account_opened is None:
            return None
        return float((extraction.tx.ts.date() - record.account_opened).days)

    def evaluate(
        self, tx: dict[str, Any], extraction: Extraction, features: Mapping[str, float]
    ) -> SignalResult:
        s = get_settings()
        w = s.app_weights
        new_payee = features.get("is_new_payee", 0.0) >= 1.0
        ratio = features.get("amount_ratio", 0.0)
        cop = self._cop(tx)
        payee_age = self._payee_age_days(tx, extraction)
        purpose = str(tx.get("purpose") or "")
        hits = purpose_keyword_hits(purpose)
        session_s = features.get("session_duration_s", -1.0)

        parts: list[tuple[str, str, float]] = []
        if new_payee and ratio >= s.app_high_amount_ratio:
            parts.append(
                ("APP_NEW_PAYEE_HIGH", "Yeni alıcıya olağandışı yüksek tutar", w["new_payee_high"])
            )
        if features.get("first_large_transfer", 0.0) >= 1.0 and new_payee:
            parts.append(
                (
                    "APP_FIRST_LARGE",
                    "Müşterinin şimdiye kadarki en büyük transferi",
                    w["first_large"],
                )
            )
        if features.get("purpose_risk", 0.0) > 0:
            parts.append(
                (
                    "APP_URGENT_TEXT",
                    "Açıklamada sosyal mühendislik ifadesi: " + ", ".join(hits[:3]),
                    w["purpose"] * features.get("purpose_risk", 0.0),
                )
            )
        if features.get("active_call", 0.0) >= 1.0 and new_payee:
            parts.append(
                ("APP_ACTIVE_CALL", "İşlem sırasında aktif telefon görüşmesi", w["active_call"])
            )
        if features.get("remote_access", 0.0) >= 1.0:
            parts.append(("APP_REMOTE_ACCESS", "Uzaktan erişim aracı açık", w["remote_access"]))
        if new_payee and session_s >= s.app_long_session_s:
            parts.append(
                (
                    "APP_LONG_SESSION",
                    "Olağandışı uzun oturum (yönlendirme şüphesi)",
                    w["long_session"],
                )
            )
        if new_payee and features.get("vulnerable_customer", 0.0) >= 1.0:
            parts.append(
                ("APP_VULNERABLE", "Yaşlı/kırılgan müşteri yeni alıcıya ödüyor", w["vulnerable"])
            )
        if cop.result == NO_MATCH:
            parts.append(
                (
                    "APP_COP_NO_MATCH",
                    "Alıcı adı IBAN sahibinin adıyla eşleşmiyor (CoP)",
                    w["cop_no_match"],
                )
            )
        elif cop.result == CLOSE:
            parts.append(
                ("APP_COP_CLOSE", "Alıcı adı IBAN sahibine yalnızca yakın (CoP)", w["cop_close"])
            )
        if payee_age is not None and payee_age < s.app_young_payee_days:
            parts.append(
                (
                    "APP_YOUNG_PAYEE",
                    f"Alıcı hesap {payee_age:.0f} gün önce açılmış",
                    w["young_payee"],
                )
            )
        if features.get("graph_known_mule_payee", 0.0) >= 1.0:
            parts.append(("APP_MULE_PAYEE", "Alıcı hesap bilinen mule ağında", w["mule_payee"]))

        score = noisy_or(weight for _, _, weight in parts)
        if not new_payee and cop.result not in (NO_MATCH,):
            score *= w["known_payee_damping"]  # APP almost always targets a new payee
        score = round(score, 4)
        warning = self._warning(features, cop, purpose)
        confirm = score >= s.app_confirm_threshold
        reasons = (
            [ReasonCode(code, text, "signal", weight) for code, text, weight in parts]
            if score >= s.app_confirm_threshold
            else []
        )
        details = {
            "score": score,
            "cop": cop.as_dict(),
            "payee_account_age_d": payee_age,
            "warning": warning if confirm else None,
            "confirmation_required": confirm,
            "patterns": [code for code, _, _ in parts],
        }
        exposed = {
            "app_score": score,
            "cop_no_match": 1.0 if cop.result == NO_MATCH else 0.0,
            "cop_unknown": 1.0 if cop.result == UNKNOWN else 0.0,
            "payee_account_age_d": payee_age if payee_age is not None else -1.0,
        }
        return SignalResult(score, reasons, details, exposed)

    @staticmethod
    def _warning(features: Mapping[str, float], cop: CoPResult, purpose: str) -> str:
        text = purpose.casefold()
        if any(k in text for k in _AUTHORITY):
            return WARNINGS["authority"]
        if any(k in text for k in _INVESTMENT):
            return WARNINGS["investment"]
        if features.get("remote_access", 0.0) >= 1.0:
            return WARNINGS["remote"]
        if features.get("active_call", 0.0) >= 1.0:
            return WARNINGS["call"]
        if cop.result == NO_MATCH:
            return WARNINGS["cop"]
        return WARNINGS["default"]
