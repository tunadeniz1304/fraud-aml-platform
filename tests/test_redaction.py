"""KVKK pseudonymisation tests (app/llm/redaction.py)."""

from __future__ import annotations

import pytest

from app.llm.redaction import (
    Redactor,
    StreamRestorer,
    is_valid_iban,
    is_valid_tckn,
    luhn_valid,
)


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


# --- M11: formatting variants must not slip past the pseudonymiser -----------


def test_m11_ibans_of_any_country_and_separator_are_masked() -> None:
    red = Redactor()
    out = red.redact(
        "TR33-0006-1005-1978-6457-8413-26, DE89 3704 0044 0532 0130 00, "
        "GB82WEST12345698765432, ref AB12 CDEF GHIJ KL"
    )
    assert "3704" not in out and "WEST" not in out and "8413" not in out
    assert out.count("IBAN") == 3
    assert "AB12 CDEF GHIJ KL" in out  # no valid mod-97 checksum -> not an IBAN
    assert red.restore(out).startswith("TR330006100519786457841326, DE89370400440532013000")
    assert is_valid_iban("DE89 3704 0044 0532 0130 00")
    assert not is_valid_iban("DE89 3704 0044 0532 0130 01")


def test_m11_card_numbers_are_masked_when_luhn_valid() -> None:
    red = Redactor()
    out = red.redact("kart 4111 1111 1111 1111, 5555-5555-5555-4444, 4111111111111112")
    assert out == "kart KART_1, KART_2, 4111111111111112"
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")


def test_m11_spaced_or_dashed_tckn_is_masked() -> None:
    red = Redactor()
    out = red.redact("TCKN 100 000 001 46, 100-000-001-46 ve 10000000146; 123 456 789 01")
    assert out == "TCKN TCKN_1, TCKN_1 ve TCKN_1; 123 456 789 01"


@pytest.mark.parametrize(
    "phone",
    [
        "+90 532 123 45 67",
        "90 532 123 45 67",
        "0532-123-45-67",
        "0532.123.45.67",
        "0 (532) 123 45 67",
    ],
)
def test_m11_phone_formats_are_masked(phone: str) -> None:
    assert Redactor().redact(f"tel: {phone}.") == "tel: TELEFON_1."


def test_m11_names_are_caught_in_any_casing() -> None:
    red = Redactor()
    out = red.redact("ayşe yılmaz ve AYŞE YILMAZ; Deniz Kaya")
    assert out == "KISI_1 ve KISI_2; KISI_3"
    # everyday words that are also first names stay when lowercase
    assert (
        red.redact("can sıkıntısı, deniz kenarı, ali ile") == "can sıkıntısı, deniz kenarı, ali ile"
    )
