"""KVKK pseudonymisation tests (app/llm/redaction.py)."""

from __future__ import annotations

import pytest

from app.llm.redaction import Redactor, StreamRestorer, is_valid_tckn


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("10000000146", True),
        ("12345678901", False),  # checksum fails
        ("02345678901", False),  # cannot start with 0
        ("1000000014", False),  # too short
        ("1000000014a", False),
    ],
)
def test_tckn_checksum(value: str, valid: bool) -> None:
    assert is_valid_tckn(value) is valid


def test_all_pii_types_are_pseudonymised_and_restored() -> None:
    red = Redactor(["Ayşe Yılmaz", "Ali Şahin"])
    text = (
        "AYŞE YILMAZ (TCKN 10000000146) TR33 0006 1005 1978 6457 8413 26 IBAN'ından "
        "Ali  Şahin'e 0532 123 45 67 / ali.sahin@example.com üzerinden ulaştı. "
        "Sipariş no 12345678901."
    )
    out = red.redact(text)
    for secret in ("AYŞE", "Şahin", "10000000146", "8413", "0532", "ali.sahin@example.com"):
        assert secret not in out
    assert "MUSTERI_1" in out and "MUSTERI_2" in out
    assert "IBAN_…1326" in out and "TCKN_1" in out and "TELEFON_1" in out and "EPOSTA_1" in out
    assert "12345678901" in out  # not a valid TCKN -> left untouched
    restored = red.restore(out)
    assert "Ayşe Yılmaz" in restored and "10000000146" in restored
    assert "TR330006100519786457841326" in restored


def test_same_value_gets_same_placeholder() -> None:
    red = Redactor(["Ayşe Yılmaz"])
    out = red.redact("Ayşe Yılmaz ve ayşe yılmaz aynı kişi")
    assert out.count("MUSTERI_1") == 2
    assert "MUSTERI_2" not in out


def test_restore_does_not_confuse_prefix_placeholders() -> None:
    red = Redactor([f"Kişi Numara{i}" for i in range(12)])
    text = " ".join(f"Kişi Numara{i}" for i in range(12))
    out = red.redact(text)
    assert "MUSTERI_10" in out
    assert red.restore(out) == text


def test_iban_last4_collision_gets_distinct_placeholders() -> None:
    red = Redactor()
    out = red.redact("TR330006100519786457841326 ve TR990001000000000000001326")
    assert "IBAN_…1326" in out and "IBAN2_…1326" in out
    assert red.restore(out) == "TR330006100519786457841326 ve TR990001000000000000001326"


def test_redact_obj_is_recursive_and_keeps_keys() -> None:
    red = Redactor(["Ayşe Yılmaz"])
    data = {"musteri": "Ayşe Yılmaz", "liste": ["ayse@example.com", 5], "n": 1.5}
    out = red.redact_obj(data)
    assert set(out) == {"musteri", "liste", "n"}
    assert out["musteri"] == "MUSTERI_1" and out["liste"][0] == "EPOSTA_1" and out["n"] == 1.5
    assert red.restore_obj(out) == data


def test_short_names_are_ignored() -> None:
    red = Redactor(["Al", ""])
    assert red.redact("Al geldi") == "Al geldi"


def test_stream_restorer_handles_split_placeholders() -> None:
    red = Redactor(["Ayşe Yılmaz"])
    red.redact("Ayşe Yılmaz")
    restorer = StreamRestorer(red)
    out = "".join(restorer.feed(c) for c in ["Merhaba MUS", "TERI", "_1, nasıl", "sın"])
    out += restorer.flush()
    assert out == "Merhaba Ayşe Yılmaz, nasılsın"
