"""SanctionScreener için odaklı testler (deterministik isim eşleştirme).

Ad + takma ad (alias) + büyük/küçük harf duyarsız eşleşme, eşleşmeme ve boş
sorgu davranışı; gerçek `data/sanctions.json` değil, tmp_path üzerindeki küçük
bir kurgusal kayıt kümesiyle çalışır.
"""

from __future__ import annotations

import json

import pytest

from app.core.sanctions import SanctionScreener

RECORDS = [
    {
        "id": "SDN-T1",
        "name": "Viktor Melnikov",
        "aliases": ["VIKTOR MELNIKOFF", "Viktor A. Melnikov"],
        "country": "RU",
        "program": "Global Magnitsky",
        "pep": True,
    },
    {
        "id": "SDN-T2",
        "name": "Karadeniz Shipping Co",
        "aliases": ["KDC Shipping"],
        "country": "UA",
        "program": "Ukraine-Related",
        "pep": False,
    },
    {
        "id": "PEP-T1",
        "name": "Hasan Abadi",
        "aliases": [],
        "country": "IR",
        "program": "PEP List",
        "pep": True,
    },
]


@pytest.fixture()
def screener(tmp_path) -> SanctionScreener:
    """tmp_path üzerinde küçük bir JSON oluşturup screener'ı yükler."""
    path = tmp_path / "sanctions.json"
    path.write_text(json.dumps(RECORDS), encoding="utf-8")
    return SanctionScreener(path).load()


def test_exact_name_match(screener: SanctionScreener) -> None:
    """Birebir ad sorgusu ilgili kaydı dosya sırasıyla döndürür."""
    candidates = screener.find_candidates("Viktor Melnikov")
    assert [c["id"] for c in candidates] == ["SDN-T1"]
    assert candidates[0]["name"] == "Viktor Melnikov"


def test_alias_match(screener: SanctionScreener) -> None:
    """Takma ad (alias) üzerinden eşleşme de aynı kaydı bulur."""
    assert screener.name_matches("VIKTOR MELNIKOFF")
    assert screener.find_candidates("KDC Shipping")[0]["id"] == "SDN-T2"


def test_case_insensitive_and_extra_whitespace(screener: SanctionScreener) -> None:
    """Büyük/küçük harf ve fazladan boşluk eşleşmeyi etkilemez."""
    assert screener.name_matches("  karadeniz   shipping CO ")
    assert screener.find_candidates("vIkToR MeLnIkOv")[0]["id"] == "SDN-T1"


def test_partial_name_via_substring(screener: SanctionScreener) -> None:
    """Kısmi isim (alt dize) eşleşmesi kaydı yakalar."""
    assert screener.name_matches("Melnikov")
    assert screener.name_matches("Abadi")


def test_no_false_substring_word_match(screener: SanctionScreener) -> None:
    """"Kara" sorgusu "Karadeniz" ile alt dize olarak eşleşmez (tam kelime)."""
    assert not screener.name_matches("Kara")
    assert not screener.name_matches("coffee")


def test_non_match_returns_empty(screener: SanctionScreener) -> None:
    """Listede olmayan bir isim için boş liste ve False döner."""
    assert screener.find_candidates("Unknown Person") == []
    assert not screener.name_matches("Ali Veli")


def test_candidates_carry_full_attributes(screener: SanctionScreener) -> None:
    """Dönen kayıtlar ülke/program/PEP gibi tüm nitelikleri taşır."""
    cand = screener.find_candidates("Hasan Abadi")[0]
    assert cand["country"] == "IR"
    assert cand["program"] == "PEP List"
    assert cand["pep"] is True
    assert all("aliases" in c and "country" in c and "pep" in c for c in screener._records)


def test_empty_and_none_query(screener: SanctionScreener) -> None:
    """Boş ya da None sorgu için [] / False döner, hata fırlatmaz."""
    assert screener.find_candidates("") == []
    assert screener.find_candidates(None) == []
    assert not screener.name_matches("")
    assert not screener.name_matches(None)


def test_unloaded_screener_is_deterministic(tmp_path) -> None:
    """Load edilmeden önce tarama tutarlı şekilde boş sonuç verir."""
    empty = SanctionScreener(tmp_path / "not-used.json")
    assert empty.find_candidates("Viktor Melnikov") == []
    assert not empty.name_matches("Viktor Melnikov")


def test_load_missing_file_raises(tmp_path) -> None:
    """Var olmayan dosya için load() doğal olarak FileNotFoundError fırlatır."""
    with pytest.raises(FileNotFoundError):
        SanctionScreener(tmp_path / "missing.json").load()


def test_default_path_points_to_data(tmp_path) -> None:
    """Varsayılan yol repo'daki data/sanctions.json dosyasına işaret eder."""
    screener = SanctionScreener()
    assert screener.path.name == "sanctions.json"
    assert screener.path.is_file()
