"""Helpers for file download responses."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

_UNSAFE_ASCII = re.compile(r"[^A-Za-z0-9._ -]")


def content_disposition(filename: str, *, fallback: str = "dosya") -> str:
    """``attachment`` header with an ASCII ``filename`` and RFC 5987 ``filename*``.

    The ASCII form drops quotes, backslashes, control characters and non-ASCII
    letters (header injection / latin-1 encoding errors); the UTF-8 form keeps
    the original name percent-encoded for clients that support it.
    """
    name = filename.strip() or fallback
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = _UNSAFE_ASCII.sub("_", folded).strip() or fallback
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
