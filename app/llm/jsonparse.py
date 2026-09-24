"""Robust JSON-object extraction from model text (no naive ``{``…``}`` slicing).

Handles Markdown code fences, leading prose and trailing commentary by trying
``json.JSONDecoder.raw_decode`` at every ``{`` until a complete object parses.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class JSONExtractionError(ValueError):
    """No JSON object could be decoded from the text."""


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON object embedded in ``text``."""
    if not text or not text.strip():
        raise JSONExtractionError("boş yanıt")
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        stripped = candidate.strip()
        for start in (i for i, ch in enumerate(stripped) if ch == "{"):
            try:
                obj, _ = decoder.raw_decode(stripped, start)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise JSONExtractionError("yanıtta geçerli bir JSON nesnesi bulunamadı")
