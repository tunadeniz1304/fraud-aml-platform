"""Confirmation / Verification of Payee (CoP) simulation (P1.3).

Before an outgoing payment the typed beneficiary name is compared with the
name registered for the IBAN:

* ``EXACT``     — names match (after Turkish/case/punctuation normalisation);
* ``CLOSE``     — a close match (typo, missing middle name) → show the masked
  registered name and ask the customer to check;
* ``NO_MATCH``  — different person/company → strong APP scam signal;
* ``UNKNOWN``   — IBAN not in the registry (other bank without CoP) → neutral.

The registry is built from the bank's own customers plus the (fictional)
payee registry file (``data/payees.json`` or ``data/demo/payees.json``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from app.core.sanctions import _fuzzy_form

EXACT, CLOSE, NO_MATCH, UNKNOWN = "EXACT", "CLOSE", "NO_MATCH", "UNKNOWN"


@dataclass(frozen=True)
class PayeeRecord:
    iban: str
    name: str
    account_opened: date | None = None
    owner_customer_id: str | None = None


@dataclass(frozen=True)
class CoPResult:
    result: str
    similarity: float
    registered_name_masked: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "similarity": round(self.similarity, 4),
            "registered_name_masked": self.registered_name_masked,
        }


def mask_name(name: str) -> str:
    """ "Ayşe Yılmaz" -> "A*** Y*****" (CoP never reveals the full name)."""
    return " ".join(part[0] + "*" * max(1, len(part) - 1) for part in name.split() if part)


class PayeeRegistry:
    def __init__(self, records: Iterable[PayeeRecord] = ()) -> None:
        self._by_iban: dict[str, PayeeRecord] = {}
        for record in records:
            self.add(record)

    def add(self, record: PayeeRecord) -> None:
        self._by_iban[record.iban.replace(" ", "").upper()] = record

    def __len__(self) -> int:
        return len(self._by_iban)

    def lookup(self, iban: str | None) -> PayeeRecord | None:
        if not iban:
            return None
        return self._by_iban.get(iban.replace(" ", "").upper())

    @classmethod
    def build(
        cls, customers: Iterable[dict[str, Any]], payees_path: Path | None = None
    ) -> PayeeRegistry:
        registry = cls()
        if payees_path is not None and payees_path.is_file():
            for row in json.loads(payees_path.read_text(encoding="utf-8")):
                opened = row.get("account_opened")
                registry.add(
                    PayeeRecord(
                        iban=str(row["iban"]),
                        name=str(row.get("name", "")),
                        account_opened=date.fromisoformat(opened) if opened else None,
                        owner_customer_id=row.get("owner_customer_id"),
                    )
                )
        for customer in customers:
            iban = customer.get("iban")
            if iban and registry.lookup(iban) is None:
                registry.add(
                    PayeeRecord(
                        iban=str(iban),
                        name=str(customer.get("name", "")),
                        owner_customer_id=str(customer["customer_id"]),
                    )
                )
        return registry

    def confirm(self, iban: str | None, typed_name: str | None) -> CoPResult:
        record = self.lookup(iban)
        if record is None or not typed_name:
            return CoPResult(UNKNOWN, 0.0)
        a, b = _fuzzy_form(typed_name), _fuzzy_form(record.name)
        if not a or not b:
            return CoPResult(UNKNOWN, 0.0)
        similarity = max(JaroWinkler.normalized_similarity(a, b), fuzz.token_sort_ratio(a, b) / 100)
        if a == b or similarity >= 0.97:
            return CoPResult(EXACT, similarity)
        if similarity >= 0.85:
            return CoPResult(CLOSE, similarity, mask_name(record.name))
        return CoPResult(NO_MATCH, similarity, mask_name(record.name))
