"""Yaptırım (sanction) listesi taraması.

``data/sanctions.json`` içinde tutulan kurgusal OFAC-tarzı yaptırım ve PEP
(önemli siyasi kişi) kayıtlarına karşı isim eşleştirmesi yapar. Müşteri
işlemlerindeki ``name`` alanları bu liste ile karşılaştırılarak yaptırımlı bir
kişi/kurumla olası bir isim çakışması varsa aday kayıt(lar) döndürülür.

Deterministik, indeksli eşleştirme (v2):

0. **Ön indeks (blocking)** — her ad/takma ad kelimesi üç anahtarla
   indekslenir: kelimenin kendisi, fonetik anahtarı (Türkçe'ye uyarlanmış
   soundex) ve karakter 3-gramları. Bulanık karşılaştırma yalnızca en az bir
   fonetik anahtarı ya da ``sanctions_min_shared_ngrams`` 3-gramı paylaşan
   adaylarla yapılır; lineer tarama yoktur.
1. **Tam kelime** — sorgunun tüm kelimeleri ad/takma adın kelime kümesinde
   (kısmi isim "Melnikov" yakalanır, "Kara" -> "Karadeniz" yakalanmaz);
2. **Bulanık** (``rapidfuzz``) — en az iki kelimelik sorgularda Jaro-Winkler
   ve sıralı-token benzerliğinin büyüğü ``sanctions_fuzzy_threshold``'u
   aşarsa ("Viktor Melnikof", "Hasan Al Abadi"). Tek kelimelik sorgular için
   **kontrollü** bulanık eşleşme: daha yüksek eşik
   (``sanctions_single_token_threshold``) **ve** eşleşen bir ikincil anahtar
   (doğum yılı ya da uyruk) şarttır.
3. **Güven** — ``confidence`` eşleşme skorundan başlar; ikincil anahtarlar
   eşleşirse artar, çelişirse düşer (``sanctions_*`` ayarları). Farklı doğum
   tarihli bir adaş aynı isimle eşleşir ama düşük güvenle raporlanır.

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


_SOUNDEX = {
    **dict.fromkeys("bfpv", "1"),
    **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"),
    "l": "4",
    **dict.fromkeys("mn", "5"),
    "r": "6",
}


def phonetic(token: str) -> str:
    """Soundex-style key (first letter + consonant classes), Turkish-folded."""
    word = _fuzzy_form(token).replace(" ", "")
    if not word:
        return ""
    out, last = [word[0]], _SOUNDEX.get(word[0], "")
    for ch in word[1:]:
        code = _SOUNDEX.get(ch, "")
        if code and code != last:
            out.append(code)
        last = code
    return "".join(out)[:4].ljust(4, "0")


def ngrams(token: str, n: int = 3) -> set[str]:
    word = f"_{_fuzzy_form(token).replace(' ', '')}_"
    return {word[i : i + n] for i in range(max(1, len(word) - n + 1))}


def _year(value: object) -> int | None:
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else None


class SanctionScreener:
    """Yaptırım listesine karşı isim taraması yapan sınıf.

    Bir kayıt, sorgu (``name``) aşağıdaki durumlarda onunla eşleşir:

    - sorgunun tüm kelimeleri, adın ya da bir takma adın kelime kümesinde
      birebir (tam kelime) bulunursa — "Melnikov" -> "Viktor Melnikov",
      ama "Kara" -> "Karadeniz" değil;
    - bulanık benzerlik eşiği aşılırsa (modül belgesindeki kurallarla).
    """

    def __init__(self, path: str | Path | None = None, *, fuzzy_threshold: float | None = None):
        """``path`` verilmemişse varsayılan ``data/sanctions.json`` kullanılır."""
        settings = config.get_settings()
        self.path: Path = Path(path) if path else DEFAULT_SANCTIONS_PATH
        self.fuzzy_threshold = (
            fuzzy_threshold if fuzzy_threshold is not None else settings.sanctions_fuzzy_threshold
        )
        self.single_token_threshold = settings.sanctions_single_token_threshold
        self.min_shared_ngrams = settings.sanctions_min_shared_ngrams
        self._records: list[dict] = []
        self._forms: list[list[str]] = []  # record index -> fuzzy forms of name + aliases
        self._token_sets: list[list[frozenset[str]]] = []
        self._by_token: dict[str, set[int]] = {}
        self._by_phonetic: dict[str, set[int]] = {}
        self._by_ngram: dict[str, set[int]] = {}

    def load(self) -> SanctionScreener:
        """JSON dosyasını okuyup kayıtları ve blocking indeksini yükler."""
        with self.path.open("r", encoding="utf-8") as fh:
            self._records = json.load(fh)
        self._forms, self._token_sets = [], []
        self._by_token, self._by_phonetic, self._by_ngram = {}, {}, {}
        for i, record in enumerate(self._records):
            names = [record["name"], *record.get("aliases", [])]
            self._forms.append([_fuzzy_form(n) for n in names])
            self._token_sets.append([frozenset(_normalise(n).split()) for n in names])
            for name in names:
                for token in _normalise(name).split():
                    self._by_token.setdefault(token, set()).add(i)
                for token in _fuzzy_form(name).split():
                    self._by_phonetic.setdefault(phonetic(token), set()).add(i)
                    for gram in ngrams(token):
                        self._by_ngram.setdefault(gram, set()).add(i)
        return self

    # --- blocking --------------------------------------------------------------------
    def _exact_ids(self, name: str) -> list[int]:
        query = _tokens(name)
        if not query:
            return []
        ids = set.intersection(*(self._by_token.get(t, set()) for t in query))
        return sorted(
            i for i in ids if any(query.issubset(tokens) for tokens in self._token_sets[i])
        )

    def candidates(self, name: str) -> set[int]:
        """Records sharing a phonetic key or enough 3-grams with any query token."""
        out: set[int] = set()
        for token in _fuzzy_form(name).split():
            out |= self._by_phonetic.get(phonetic(token), set())
            counts: dict[int, int] = {}
            for gram in ngrams(token):
                for i in self._by_ngram.get(gram, ()):
                    counts[i] = counts.get(i, 0) + 1
            out |= {i for i, c in counts.items() if c >= self.min_shared_ngrams}
        return out

    # --- API -----------------------------------------------------------------------------
    def find_candidates(self, name: str | None) -> list[dict]:
        """``name`` ile tam-kelime eşleşen kayıtlar (dosya sırasıyla)."""
        if not name:
            return []
        return [self._records[i] for i in self._exact_ids(name)]

    def fuzzy_score(self, query: str, target: str) -> float:
        """0..1 similarity (max of Jaro-Winkler and token-sort ratio)."""
        q, t = _fuzzy_form(query), _fuzzy_form(target)
        if not q or not t:
            return 0.0
        return max(JaroWinkler.normalized_similarity(q, t), fuzz.token_sort_ratio(q, t) / 100)

    @staticmethod
    def _secondary(
        record: dict, birth_date: str | None, nationality: str | None
    ) -> dict[str, bool | None]:
        """True = matches, False = contradicts, None = unknown on either side."""
        listed_year = _year(record.get("birth_date") or record.get("birth_year"))
        query_year = _year(birth_date)
        dob = None if listed_year is None or query_year is None else listed_year == query_year
        listed_country = str(record.get("nationality") or record.get("country") or "").upper()
        nat = None
        if listed_country and nationality:
            nat = listed_country == nationality.upper()
        return {"birth_date": dob, "nationality": nat}

    @staticmethod
    def _confidence(score: float, secondary: dict[str, bool | None]) -> float:
        s = config.get_settings()
        conf = score
        for key, bonus, factor in (
            ("birth_date", s.sanctions_dob_match_bonus, s.sanctions_dob_mismatch_factor),
            ("nationality", s.sanctions_nat_match_bonus, s.sanctions_nat_mismatch_factor),
        ):
            if secondary[key] is True:
                conf = min(1.0, conf + bonus)
            elif secondary[key] is False:
                conf *= factor
        return round(conf, 4)

    def screen(
        self,
        name: str | None,
        *,
        birth_date: str | None = None,
        nationality: str | None = None,
    ) -> list[dict]:
        """Exact-token + fuzzy matches with ``match_type``, ``match_score`` and
        ``confidence`` (adjusted by the secondary keys)."""
        if not name:
            return []
        query = _fuzzy_form(name)
        if not query:
            return []
        exact = set(self._exact_ids(name))
        single = len(query.split()) < 2
        out: list[dict] = []
        for i in sorted(exact | self.candidates(name)):
            record = self._records[i]
            secondary = self._secondary(record, birth_date, nationality)
            if i in exact:
                match_type, score = "exact", 1.0
            else:
                score = max(self.fuzzy_score(query, f) for f in self._forms[i])
                if single:
                    # controlled single-token fuzzy: high bar + a confirming secondary key
                    if score < self.single_token_threshold or True not in secondary.values():
                        continue
                elif score < self.fuzzy_threshold:
                    continue
                match_type = "fuzzy"
            out.append(
                {
                    **record,
                    "match_type": match_type,
                    "match_score": round(score, 4),
                    "confidence": self._confidence(score, secondary),
                    "secondary": secondary,
                }
            )
        return out

    def name_matches(self, name: str | None) -> bool:
        """``name`` ile en az bir kayıt eşleşiyorsa ``True`` döndürür."""
        return bool(self.find_candidates(name))
