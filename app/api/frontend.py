"""Legacy (classic) dashboard renderer.

Fixes bug #3: the API base URL used to be concatenated into an inline script
as the literal text ``" + API + r"``. The page is now a static template whose
only dynamic value — the API base — is HTML-escaped into
``<meta name="api-base">`` and read by the external ``dashboard.js``. No inline
script or style is emitted, so the CSP needs no ``'unsafe-inline'``.
"""

from __future__ import annotations

import html
import os
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_TEMPLATE = STATIC_DIR / "legacy" / "index.html"


@lru_cache(maxsize=1)
def _template() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def render(api_base: str | None = None) -> str:
    """Return the classic dashboard HTML with the API base safely embedded."""
    base = os.getenv("API_BASE", "") if api_base is None else api_base
    return _template().replace("{{API_BASE}}", html.escape(base, quote=True))
