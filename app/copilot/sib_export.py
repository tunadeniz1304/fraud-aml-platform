"""ŞİB draft export as PDF (reportlab, Turkish-capable font when available)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
)


def _font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path in _FONT_CANDIDATES:
        if path.is_file():
            try:
                pdfmetrics.registerFont(TTFont("SibFont", str(path)))
                return "SibFont"
            except Exception:  # noqa: BLE001, S112  # pragma: no cover - corrupt font file
                continue
    return "Helvetica"  # pragma: no cover - no TTF available


def render_sib_pdf(draft: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    font = _font()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    _width, height = A4
    y = height - 50

    def line(text: str, size: int = 10, gap: int = 14) -> None:
        nonlocal y
        if y < 60:
            pdf.showPage()
            y = height - 50
        pdf.setFont(font, size)
        pdf.drawString(50, y, text[:110])
        y -= gap

    line("ŞÜPHELİ İŞLEM BİLDİRİMİ (ŞİB) — TASLAK", 14, 22)
    line(f"Vaka: {draft.get('_case', '')}   Durum: {draft.get('_status', '')}", 9)
    line(f"MASAK referansı: {draft.get('masak_reference', '-')}", 9, 20)
    for label, key in (
        ("Şüpheli işlem tipi", "supheli_islem_tipi"),
        ("Kim", "kim"),
        ("Ne", "ne"),
        ("Ne zaman", "ne_zaman"),
        ("Nerede", "nerede"),
        ("Neden", "neden"),
        ("Şüpheye ilk ulaşılma", "supheye_ilk_ulasilma"),
        ("Bildirim son tarihi", "bildirim_son_tarihi"),
    ):
        text = str(draft.get(key, ""))
        line(f"{label}:", 10, 12)
        for start in range(0, max(1, len(text)), 100):
            line("   " + text[start : start + 100], 9, 12)
    subject = draft.get("supheli") or {}
    line(f"Şüpheli: {subject.get('ad_soyad', '')} ({subject.get('musteri_no', '')})", 10, 18)
    line("İşlemler:", 10, 12)
    for tx in draft.get("islemler") or []:
        line(
            f"   {tx.get('transaction_id')}  {tx.get('tarih')}  {tx.get('tutar_try'):,.2f} TL  "
            f"{tx.get('alici', '')}",
            8,
            11,
        )
    line(f"Toplam: {float(draft.get('toplam_tutar_try') or 0):,.2f} TL", 10, 18)
    warning = str(draft.get("tipping_off_uyarisi", ""))
    for start in range(0, len(warning), 100):
        line(warning[start : start + 100], 8, 11)
    line(str(draft.get("hazirlayan", "")), 8)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
