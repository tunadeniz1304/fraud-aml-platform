"""Deterministic demo renderers for the copilot tasks (DEMO mode / fallback).

They build Turkish, evidence-grounded answers from the gathered dossier and
cite only ids present in ``allowed_citations`` — so the demo output passes
the same citation validator as a live model.
"""

from __future__ import annotations

from typing import Any

from app.llm.demo import fmt_try, register_template

SIB_TYPES = {
    "AML": "Kara para aklama şüphesi — işlemlerin eşik altına bölünmesi (smurfing)",
    "MULE": "Suç gelirlerinin aklanmasına aracılık — para katırı (mule) hesabı",
    "APP": "Nitelikli dolandırıcılık — yetkili itme ödemesi (APP) yoluyla",
    "ATO": "Bilişim sistemleri aracılığıyla dolandırıcılık — hesap ele geçirme",
    "YAPTIRIM": "Yaptırım listesi / terörün finansmanı şüphesi",
    "CARD_TESTING": "Kart bilgilerinin izinsiz kullanımı (kart testi)",
}
#: Placeholder for dossier fields not yet persisted (write-behind writer still pending).
UNKNOWN_DATE = "tarih kaydı henüz yazılmadı"
TIPPING_OFF = (
    "5549 sayılı Kanun md. 4/2 uyarınca şüpheli işlem bildiriminde bulunulduğu bilgisi, işlem "
    "tarafları dahil hiç kimseye açıklanamaz (tipping-off yasağı). Müşteriyle yapılan "
    "iletişimde bildirimden söz edilmez."
)


def _case(ctx: dict[str, Any]) -> dict[str, Any]:
    return dict(ctx.get("get_case") or {})


def _cite(ctx: dict[str, Any], *ids: str | None) -> list[str]:
    allowed = set(ctx.get("allowed_citations") or [])
    out = [i for i in ids if i and i in allowed]
    return out or sorted(allowed)[:1]


def _key_alert(case: dict[str, Any]) -> dict[str, Any]:
    alerts = case.get("alerts") or []
    return max(alerts, key=lambda a: a.get("risk_score") or 0) if alerts else {}


@register_template("copilot_investigate")
def _investigate(ctx: dict[str, Any]) -> str:
    return "İnceleme tamamlandı; kanıtlar toplandı."


@register_template("copilot_summary")
def _summary(ctx: dict[str, Any]) -> dict[str, Any]:
    case = _case(ctx)
    alert = _key_alert(case)
    profile = ctx.get("get_customer_profile") or {}
    graph = ctx.get("get_graph_neighborhood") or {}
    similar = (ctx.get("similar_past_cases") or {}).get("similar") or []
    reasons = alert.get("reason_codes") or []
    bullets = [
        {
            "text": f"{case.get('title', 'Vaka')}: {len(case.get('alerts', []))} alert, toplam "
            f"{fmt_try(case.get('total_amount_try'))}; durum {case.get('status')}.",
            "citations": _cite(ctx, case.get("case_id"), alert.get("alert_id")),
        },
        {
            "text": f"En riskli işlem {alert.get('transaction_id', '?')} (risk "
            f"{float(alert.get('risk_score') or 0):.2f}, karar {alert.get('decision')}): "
            + "; ".join(r.get("text", "") for r in reasons[:3]),
            "citations": _cite(
                ctx, alert.get("transaction_id"), *(r.get("code") for r in reasons[:3])
            ),
        },
        {
            "text": f"Müşteri {profile.get('customer_id', case.get('customer_id'))}: segment "
            f"{profile.get('segment', '?')}, ortalama işlem "
            f"{fmt_try(profile.get('avg_amount_try'))}, hesap durumu "
            f"{profile.get('account_status', '?')}"
            + (" — kırılgan/yaşlı müşteri." if profile.get("vulnerable") else "."),
            "citations": _cite(ctx, f"PROFILE-{case.get('customer_id')}", case.get("customer_id")),
        },
        {
            "text": (
                f"Graf: müşteri {graph['ring_id']} halkasında; {graph.get('nodes', 0)} düğüm, "
                f"paylaşılan cihaz: {', '.join(graph.get('shared_devices') or []) or 'yok'}."
                if graph.get("ring_id")
                else f"Graf: {graph.get('nodes', 0)} düğümlük komşulukta doğrulanmış halka yok"
                + (
                    f"; fraud bağlantısı: {', '.join(graph['fraud_nodes'])}."
                    if graph.get("fraud_nodes")
                    else "."
                )
            ),
            "citations": _cite(ctx, graph.get("ring_id"), f"GRAPH-{case.get('customer_id')}"),
        },
        {
            "text": (
                "Benzer doğrulanmış geçmiş vakalar: " + ", ".join(s["case_id"] for s in similar[:3])
                if similar
                else "Aynı tipolojide doğrulanmış geçmiş vaka yok; karar mevcut kanıtla verilmeli."
            ),
            "citations": _cite(ctx, *(s["case_id"] for s in similar[:3]), case.get("case_id")),
        },
    ]
    risk = float(alert.get("risk_score") or 0)
    level = "yüksek" if risk >= 0.85 else "orta-yüksek" if risk >= 0.6 else "orta"
    return {
        "bullets": bullets,
        "risk_assessment": f"Genel risk {level} ({risk:.2f}); vaka tipi {case.get('case_type')}.",
    }


@register_template("copilot_recommendation")
def _recommendation(ctx: dict[str, Any]) -> dict[str, Any]:
    case = _case(ctx)
    alert = _key_alert(case)
    risk = float(alert.get("risk_score") or 0)
    graph = ctx.get("get_graph_neighborhood") or {}
    timeline = case.get("timeline") or []
    denied = next(
        (
            e
            for e in timeline
            if e["type"] == "CUSTOMER_CONFIRMATION"
            and not (e.get("payload") or {}).get("knows_payee", True)
        ),
        None,
    )
    confirmed = next(
        (
            e
            for e in timeline
            if e["type"] == "CUSTOMER_CONFIRMATION" and (e.get("payload") or {}).get("knows_payee")
        ),
        None,
    )
    if case.get("case_type") == "YAPTIRIM":
        decision, conf = "INCELEMEYE_DEVAM", 0.7
        why = "Yaptırım eşleşmesi uyum ekibinin kimlik doğrulamasını gerektirir."
    elif denied or graph.get("ring_id") or risk >= 0.85:
        decision, conf = "FRAUD", round(min(0.95, 0.6 + risk / 3), 2)
        why = (
            ("Müşteri alıcıyı tanımadığını beyan etti. " if denied else "")
            + (f"Hesap {graph['ring_id']} halkasında. " if graph.get("ring_id") else "")
            + (f"En riskli işlemin skoru {risk:.2f}.")
        )
    elif confirmed and risk < 0.7:
        decision, conf = "TEMIZ", 0.65
        why = "Müşteri alıcıyı tanıdığını doğruladı ve risk sınırlı."
    else:
        decision, conf = "INCELEMEYE_DEVAM", 0.55
        why = f"Risk {risk:.2f}; müşteriyle doğrulama ve ek kanıt önerilir."
    cites = _cite(
        ctx,
        alert.get("transaction_id"),
        case.get("case_id"),
        graph.get("ring_id"),
        denied["event_id"] if denied else None,
    )
    return {
        "decision": decision,
        "confidence": conf,
        "rationale": why,
        "citations": cites,
        "next_steps": [
            "Müşteriyle kayıtlı telefondan iletişime geçin (tipping-off yasağına dikkat).",
            "Alıcı bankayla alıcı hesabı için bilgi paylaşımı talep edin.",
        ],
    }


@register_template("sib_draft")
def _sib(ctx: dict[str, Any]) -> dict[str, Any]:
    case = _case(ctx)
    profile = ctx.get("get_customer_profile") or {}
    alerts = case.get("alerts") or []
    total = sum(float(a.get("amount_try") or 0) for a in alerts)
    tx_ids = [a["transaction_id"] for a in alerts]
    kind = str(case.get("case_type") or "")
    beneficiaries = sorted(
        {str(a.get("beneficiary") or "") for a in alerts if a.get("beneficiary")}
    )
    reasons = sorted(
        {r.get("text", "") for a in alerts for r in a.get("reason_codes") or []} - {""}
    )
    joined = "; ".join(reasons[:6])
    why = (
        joined
        if len(joined) >= 10
        else "Olağandışı işlem örüntüsü" + (f": {joined}" if joined else "")
    )
    stamps = [str(a.get("ts")) for a in alerts if a.get("ts")]
    first = min(stamps, default="") or str(case.get("suspicion_at") or "")
    last = max(stamps, default="") or first
    return {
        "supheli_islem_tipi": SIB_TYPES.get(kind, "Olağandışı ve ekonomik amacı belirsiz işlem"),
        "supheli": {
            "musteri_no": case.get("customer_id", ""),
            "ad_soyad": profile.get("name") or case.get("customer_id", ""),
            "hesap": "",
            "dogum_yili": profile.get("birth_year"),
        },
        "kim": f"{case.get('customer_id') or 'bilinmeyen'} numaralı müşteri "
        f"({profile.get('segment') or 'bireysel'})",
        "ne": f"{len(alerts)} adet transfer, toplam {fmt_try(total)}; alıcılar: "
        + (", ".join(beneficiaries[:5]) or "—"),
        "ne_zaman": (f"{first} – {last}" if first != last else first) if first else UNKNOWN_DATE,
        "nerede": "Mobil/internet bankacılığı kanalları, "
        + ", ".join(sorted({str(a.get("channel") or "?") for a in alerts})),
        "neden": why,
        "islemler": [
            {
                "transaction_id": a["transaction_id"],
                "tarih": str(a.get("ts") or UNKNOWN_DATE),
                "tutar": float(a.get("amount_try") or 0),
                "para_birimi": "TRY",
                "tutar_try": float(a.get("amount_try") or 0),
                "alici": str(a.get("beneficiary") or ""),
                "aciklama": str(a.get("purpose") or ""),
            }
            for a in alerts
        ],
        "toplam_tutar_try": round(total, 2),
        "supheye_ilk_ulasilma": str(case.get("suspicion_at", "")),
        "bildirim_son_tarihi": str(case.get("masak_deadline", "")),
        "tipping_off_uyarisi": TIPPING_OFF,
        "citations": _cite(ctx, case.get("case_id"), *tx_ids),
    }


@register_template("app_text_triage")
def _triage(ctx: dict[str, Any]) -> dict[str, Any]:
    text = str(ctx.get("text", "")).casefold()
    table = (
        ("guvenli_hesap", ("güvenli hesap", "guvenli hesap", "güvenli hesab")),
        ("sahte_yetkili", ("polis", "savcı", "savci", "masak", "vergi")),
        ("yatirim", ("yatırım", "yatirim", "kripto", "getiri")),
        ("romantik", ("sevgilim", "evlilik", "bilet")),
    )
    for label, words in table:
        if any(w in text for w in words):
            return {"label": label, "confidence": 0.8}
    return {"label": "yok" if not text.strip() else "diger", "confidence": 0.5}


def minimal_sib_draft(case: dict[str, Any]) -> dict[str, Any]:
    """Deterministic, schema-safe ŞİB draft built from the case record only.

    Used when both the model output and its repair attempt were rejected: the
    analyst still gets a draft to complete instead of a silently missing one.
    """
    alerts = case.get("alerts") or []
    kind = str(case.get("case_type") or "")
    total = sum(float(a.get("amount_try") or 0) for a in alerts)
    when = str(case.get("suspicion_at") or "") or UNKNOWN_DATE
    return {
        "supheli_islem_tipi": SIB_TYPES.get(kind, "Olağandışı ve ekonomik amacı belirsiz işlem"),
        "supheli": {"musteri_no": str(case.get("customer_id") or ""), "ad_soyad": ""},
        "kim": f"{case.get('customer_id') or 'bilinmeyen'} numaralı müşteri",
        "ne": f"{len(alerts)} adet işlem, toplam {fmt_try(total)}",
        "ne_zaman": when if len(when) >= 5 else UNKNOWN_DATE,
        "nerede": "Dijital bankacılık kanalları",
        "neden": "Otomatik taslak üretilemedi; analist gerekçeyi vaka kanıtlarından doldurmalı.",
        "islemler": [
            {
                "transaction_id": str(a.get("transaction_id")),
                "tarih": str(a.get("created_at") or UNKNOWN_DATE),
                "tutar": float(a.get("amount_try") or 0),
                "tutar_try": float(a.get("amount_try") or 0),
            }
            for a in alerts
        ]
        or [{"transaction_id": "-", "tarih": UNKNOWN_DATE, "tutar": 0.0, "tutar_try": 0.0}],
        "toplam_tutar_try": round(total, 2),
        "supheye_ilk_ulasilma": str(case.get("suspicion_at") or ""),
        "bildirim_son_tarihi": str(case.get("masak_deadline") or ""),
        "tipping_off_uyarisi": TIPPING_OFF,
        "citations": [f"CASE-{case.get('id')}"],
        "hazirlayan": "Anil3 (minimal deterministik taslak — analist tamamlamalı)",
    }
