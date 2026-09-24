"""Yaptırım (sanction) listesi taraması.

``data/sanctions.json`` içinde tutulan kurgusal OFAC-tarzı yaptırım ve PEP
(önemli siyasi kişi) kayıtlarına karşı isim eşleştirmesi yapar. Müşteri
işlemlerindeki ``name`` alanları bu liste ile karşılaştırılarak yaptırımlı bir
kişi/kurumla olası bir isim çakışması varsa aday kayıt(lar) döndürülür.

İki aşamalı, deterministik eşleştirme:

1. **Tam kelime** — sorgunun tüm kelimeleri ad/takma adın kelime kümesinde
   (kısmi isim "Melnikov" yakalanır, "Kara" -> "Karadeniz" yakalanmaz);
2. **Bulanık** (P0.5, ``rapidfuzz``) — en az iki kelimelik sorgularda
   Jaro-Winkler ve sıralı-token benzerliğinin büyüğü eşiği
   (``SANCTIONS_FUZZY_THRESHOLD``, varsayılan 0.93) aşarsa: yazım hatası ve
   transliterasyon varyantları ("Viktor Melnikof", "Hasan Al Abadi").

Türkçe karakterler aksansız biçime indirgenir. Modül import-safe'dir; dosya
yalnızca ``load()`` çağrıldığında okunur.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from app import config

# Varsayılan veri dosyası: ``app/config.py`` içinde tanımlı ``DATA_DIR``.
DEFAULT_SANCTIONS_PATH = config.DATA_DIR / "sanctions.json"


def _normalise(text: str) -> str:
    """Metni büyük/küçük harf duyarsız karşılaştırmaya hazırlar.

    Büyük/küçük harf (casefold) ve fazladan boşluklar tek boşluğa indirilir —
    "Viktor  Melnikov" ile "VIKTOR melnikov" aynı normal biçimi üretir.
    Birleşik aksan karakterleri (örn. "İ" casefold sonrası "i"+U+0307) NFD ile
    ayrıştırılıp atılır; böylece Türkçe karakter varyantları da eşleşir.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.replace("ı", "i").split())  # Türkçe noktasız ı -> i


_PUNCT = re.compile(r"[^\w\s]")


def _fuzzy_form(text: str) -> str:
    """Normalised text without punctuation ("Myung-soo" -> "myung soo")."""
    return " ".join(_PUNCT.sub(" ", _normalise(text)).split())


def _tokens(text: str) -> set[str]:
    """Normalize edilmiş metnin kelime kümesini döndürür."""
    return set(_normalise(text).split())


class SanctionScreener:
    """Yaptırım listesine karşı isim taraması yapan sınıf.

    Bir kayıt, sorgu (``name``) aşağıdaki durumlarda onunla eşleşir:

    - sorgu, adın ya da bir takma adın tam bir kelimesiyse (kelime-sınırı
      alt dize eşleşmesi) — "Melnikov" -> "Viktor Melnikov", VEYA
    - sorgunun tüm kelimeleri, adın ya da bir takma adın kelime kümesinde
      birebir (tam kelime) bulunursa.

    Kelime-sınırı eşleşmesi kısmi isimleri ("Melnikov") yakalarken, bir
    kelimenin ortasına karşılık gelen kısmî alt dizeyi ("Kara" in
    "Karadeniz") yanlış-pozitif olarak kabul etmez.
    """

    def __init__(self, path: str | Path | None = None, *, fuzzy_threshold: float | None = None):
        """``path`` verilmemişse varsayılan ``data/sanctions.json`` kullanılır."""
        self.path: Path = Path(path) if path else DEFAULT_SANCTIONS_PATH
        self.fuzzy_threshold = (
            fuzzy_threshold
            if fuzzy_threshold is not None
            else config.get_settings().sanctions_fuzzy_threshold
        )
        self._records: list[dict] = []
        self._index: list[tuple[dict, list[frozenset[str]]]] = []
        self._fuzzy: list[tuple[dict, list[str]]] = []

    def load(self) -> SanctionScreener:
        """JSON dosyasını okuyup kayıtları yükler; kendisini döndürür.

        Dosya yoksa ``FileNotFoundError`` doğal olarak fırlar. Zincirleme
        çağrı için akıcı (fluent) kullanım sunar: ``screener.load().find(...)``.
        """
        with self.path.open("r", encoding="utf-8") as fh:
            self._records = json.load(fh)
        # Normalise every name/alias once (screening runs on the hot path).
        self._index = [
            (
                record,
                [
                    frozenset(_normalise(n).split())
                    for n in [record["name"], *record.get("aliases", [])]
                ],
            )
            for record in self._records
        ]
        self._fuzzy = [
            (record, [_fuzzy_form(n) for n in [record["name"], *record.get("aliases", [])]])
            for record in self._records
        ]
        return self

    def _matches(self, query: str, target: str) -> bool:
        """Tek bir hedef ad/takma ad için eşleşme kontrolü."""
        q = _normalise(query)
        t = _normalise(target)
        if not q or not t:
            return False
        # Tam-kelime (full-word) eşleşmesi: sorgunun tüm kelimeleri hedefin
        # kelime kümesinde birebir bulunmalı. "Melnikov" -> "Viktor Melnikov",
        # ama "Kara" -> "Karadeniz" YANLIŞ POZİTİF üretmez. Regex \b kullanmak
        # Türkçe "İ" gibi casefold sonrası birleşik aksan karakterleriyle bozulur,
        # bu yüzden kelime-kümesi karşılaştırması kullanılır.
        query_tokens = _tokens(query)
        return bool(query_tokens) and query_tokens.issubset(t.split())

    def find_candidates(self, name: str | None) -> list[dict]:
        """``name`` ile eşleşen kayıtların listesini dosya sırasıyla döndürür."""
        if not name:
            return []
        query = _tokens(name)
        if not query:
            return []
        return [
            record
            for record, token_sets in self._index
            if any(query.issubset(tokens) for tokens in token_sets)
        ]

    def fuzzy_score(self, query: str, target: str) -> float:
        """0..1 similarity (max of Jaro-Winkler and token-sort ratio)."""
        q, t = _fuzzy_form(query), _fuzzy_form(target)
        if not q or not t:
            return 0.0
        return max(JaroWinkler.normalized_similarity(q, t), fuzz.token_sort_ratio(q, t) / 100)

    def screen(self, name: str | None) -> list[dict]:
        """Exact-token + fuzzy matches with ``match_type`` and ``match_score``."""
        if not name:
            return []
        exact_ids = {id(r) for r in self.find_candidates(name)}
        out: list[dict] = []
        query = _fuzzy_form(name)
        multi_token = len(query.split()) >= 2
        for record, forms in self._fuzzy:
            if id(record) in exact_ids:
                out.append({**record, "match_type": "exact", "match_score": 1.0})
                continue
            if not multi_token:
                continue
            score = max(
                max(
                    JaroWinkler.normalized_similarity(query, f),
                    fuzz.token_sort_ratio(query, f) / 100,
                )
                for f in forms
            )
            if score >= self.fuzzy_threshold:
                out.append({**record, "match_type": "fuzzy", "match_score": round(score, 4)})
        return out

    def name_matches(self, name: str | None) -> bool:
        """``name`` ile en az bir kayıt eşleşiyorsa ``True`` döndürür."""
        return bool(self.find_candidates(name))
