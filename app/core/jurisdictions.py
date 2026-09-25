"""High-risk jurisdictions from the versioned FATF list (docs/COMPLIANCE.md).

``data/jurisdictions/fatf_<yyyy-mm>.json`` holds the FATF "High-Risk
Jurisdictions subject to a Call for Action" and "Jurisdictions under Increased
Monitoring" lists of one plenary, with source URLs and a per-list verification
note. ``high_risk_lists`` selects which lists feed ``is_high_risk_country``;
``high_risk_countries_extra`` adds bank-specific jurisdictions (empty by
default). A new plenary is a new file — the old one stays for audit.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings


@lru_cache(maxsize=4)
def _read(path: str) -> dict[str, Any]:
    return dict(json.loads(Path(path).read_text(encoding="utf-8")))


def load_jurisdictions(path: Path | None = None) -> dict[str, Any]:
    return _read(str(path or get_settings().resolved_jurisdictions_path))


@lru_cache(maxsize=8)
def _codes(path: str, lists: tuple[str, ...], extra: str) -> frozenset[str]:
    data = _read(path)
    codes = {c.upper() for name in lists for c in data["lists"].get(name, {}).get("countries", [])}
    codes |= {c.strip().upper() for c in extra.split(",") if c.strip()}
    return frozenset(codes)


def high_risk_codes() -> frozenset[str]:
    s = get_settings()
    return _codes(
        str(s.resolved_jurisdictions_path), tuple(s.high_risk_lists), s.high_risk_countries_extra
    )
