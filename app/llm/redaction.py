"""KVKK pseudonymisation before any text leaves for an external LLM (§3.3).

TCKN (checksum-validated), IBAN, phone, e-mail and known person names are
replaced by stable placeholders (``MUSTERI_7``, ``IBAN_…1234``, ``TCKN_1`` …);
the mapping stays in process memory for one call and the model's answer is
mapped back with :meth:`Redactor.restore`. Placeholders are chosen so that no
original value can be reconstructed from them.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

_IBAN_RE = re.compile(r"\bTR\d{2}(?:[ ]?\d{4}){5}[ ]?\d{2}\b", re.IGNORECASE)
_TCKN_RE = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+90[ ]?|0)?5\d{2}[ ]?\d{3}[ ]?\d{2}[ ]?\d{2}(?!\d)")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def is_valid_tckn(value: str) -> bool:
    """Official T.C. Kimlik No checksum (10th and 11th digit rules)."""
    if len(value) != 11 or not value.isdigit() or value[0] == "0":
        return False
    d = [int(c) for c in value]
    tenth = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    eleventh = sum(d[:10]) % 10
    return d[9] == tenth and d[10] == eleventh


def _fold(text: str) -> str:
    """Case/diacritic-insensitive key (İ/ı/ş/ğ → i/i/s/g)."""
    decomposed = unicodedata.normalize("NFD", text.replace("ı", "i").casefold())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


class Redactor:
    """Reversible, per-call pseudonymiser."""

    def __init__(self, names: Iterable[str] = ()) -> None:
        self._forward: dict[str, str] = {}  # original -> placeholder
        self._reverse: dict[str, str] = {}  # placeholder -> original
        self._counters: dict[str, int] = {}
        # Longest names first so "Ayşe Yılmaz Kaya" wins over "Ayşe Yılmaz".
        cleaned = {n.strip() for n in names if n and len(n.strip()) >= 3}
        self._names = sorted(cleaned, key=len, reverse=True)

    # --- placeholder bookkeeping -------------------------------------------
    def _placeholder(self, kind: str, original: str, suffix: str = "") -> str:
        key = f"{kind}:{_fold(original) if kind == 'MUSTERI' else original}"
        if key in self._forward:
            return self._forward[key]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        n = self._counters[kind]
        if kind == "IBAN":
            token = f"IBAN_…{suffix}" if n == 1 else f"IBAN{n}_…{suffix}"
            while token in self._reverse:  # identical last-4 collision
                n += 1
                token = f"IBAN{n}_…{suffix}"
        else:
            token = f"{kind}_{n}"
        self._forward[key] = token
        self._reverse[token] = original
        return token

    # --- public API ----------------------------------------------------------
    @property
    def mapping(self) -> dict[str, str]:
        """placeholder → original (for tests / audit, never sent anywhere)."""
        return dict(self._reverse)

    def _iban(self, match: re.Match[str]) -> str:
        normalised = re.sub(r"\s", "", match.group(0)).upper()
        return self._placeholder("IBAN", normalised, normalised[-4:])

    def redact(self, text: str) -> str:
        if not text:
            return text
        out = _IBAN_RE.sub(self._iban, text)
        out = _EMAIL_RE.sub(lambda m: self._placeholder("EPOSTA", m.group(0)), out)
        out = _TCKN_RE.sub(
            lambda m: (
                self._placeholder("TCKN", m.group(0)) if is_valid_tckn(m.group(0)) else m.group(0)
            ),
            out,
        )
        out = _PHONE_RE.sub(lambda m: self._placeholder("TELEFON", m.group(0)), out)
        for name in self._names:
            pattern = re.compile(
                r"(?<!\w)" + r"\s+".join(re.escape(p) for p in name.split()) + r"(?!\w)",
                re.IGNORECASE,
            )
            token = self._placeholder("MUSTERI", name) if pattern.search(out) else None
            if token is not None:
                out = pattern.sub(token, out)
        return out

    def redact_obj(self, value: Any) -> Any:
        """Recursively redact every string inside dicts/lists (keys preserved)."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_obj(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.redact_obj(v) for v in value]
        return value

    def restore(self, text: str) -> str:
        if not text or not self._reverse:
            return text
        tokens = sorted(self._reverse, key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(t) + r"(?![0-9])" for t in tokens))
        return pattern.sub(lambda m: self._reverse[m.group(0)], text)

    def restore_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.restore(value)
        if isinstance(value, dict):
            return {k: self.restore_obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore_obj(v) for v in value]
        return value


class StreamRestorer:
    """Restore placeholders in a token stream even when split across chunks.

    The trailing, possibly incomplete word is held back until whitespace (or
    :meth:`flush`) arrives, so ``"MUSTE" + "RI_1"`` is still mapped back.
    """

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._pending = ""

    def feed(self, chunk: str) -> str:
        text = self._pending + chunk
        cut = max(text.rfind(" "), text.rfind("\n"))
        if cut < 0:
            self._pending = text
            return ""
        self._pending = text[cut + 1 :]
        return self._redactor.restore(text[: cut + 1])

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        return self._redactor.restore(rest)
