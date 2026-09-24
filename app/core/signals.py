"""Context signals: offline IP intelligence and purpose-text keywords.

* :class:`IPIntel` — tiny offline IP→country/ASN table with VPN/Tor flags
  (``data/ip_intel.json``, fictional demo data; no external GeoIP service).
* :func:`purpose_keyword_score` — APP-scam / urgency keyword weight of a
  transfer description (``rules/app_keywords.yaml``).
"""

from __future__ import annotations

import ipaddress
import json
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import BASE_DIR, get_settings


@dataclass(frozen=True)
class IPInfo:
    country: str
    asn: int
    org: str
    vpn: bool
    tor: bool

    @property
    def anonymised(self) -> bool:
        return self.vpn or self.tor


class IPIntel:
    """Longest-prefix lookup over a small CIDR table."""

    def __init__(self, path: Path | None = None) -> None:
        path = path or get_settings().data_dir / "ip_intel.json"
        self._ranges: list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, IPInfo]] = []
        if path.is_file():
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            for row in data.get("ranges", []):
                net = ipaddress.ip_network(row["cidr"], strict=False)
                info = IPInfo(
                    country=row["country"],
                    asn=int(row["asn"]),
                    org=row.get("org", ""),
                    vpn=bool(row.get("vpn")),
                    tor=bool(row.get("tor")),
                )
                self._ranges.append((net, info))
            self._ranges.sort(key=lambda item: item[0].prefixlen, reverse=True)

    def lookup(self, ip: str | None) -> IPInfo | None:
        if not ip:
            return None
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            return None
        for net, info in self._ranges:
            if addr.version == net.version and addr in net:
                return info
        return None


@lru_cache(maxsize=1)
def default_ip_intel() -> IPIntel:
    return IPIntel()


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.replace("İ", "i").replace("I", "ı").casefold())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("ı", "i")


@lru_cache(maxsize=1)
def _keywords() -> dict[str, float]:
    path = BASE_DIR / "rules" / "app_keywords.yaml"
    if not path.is_file():  # pragma: no cover - shipped with the repo
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {_fold(str(k)): float(v) for k, v in (data.get("keywords") or {}).items()}


def purpose_keyword_hits(purpose: str | None) -> list[str]:
    text = _fold(purpose or "")
    return [kw for kw in _keywords() if kw in text]


def purpose_keyword_score(purpose: str | None) -> float:
    """0..1 urgency/APP keyword score of a free-text transfer description."""
    table = _keywords()
    return min(1.0, sum(table[kw] for kw in purpose_keyword_hits(purpose)))
