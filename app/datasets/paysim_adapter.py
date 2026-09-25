"""PaySim → platform ``Transaction`` mapping (docs/DATA.md).

PaySim (Lopez-Rojas et al., 2016; CC BY 4.0) simulates a month of mobile-money
logs calibrated on a real operator. One row::

    step,type,amount,nameOrig,oldbalanceOrg,newbalanceOrig,nameDest,
    oldbalanceDest,newbalanceDest,isFraud,isFlaggedFraud

Mapping rules:

* ``step`` (hour index, 1–743) → ``ts`` = ``paysim_base_date`` + ``step`` hours;
  rows of the same hour keep their file order through a seconds offset;
* ``type`` → channel + purpose (:data:`TYPE_MAP`);
* ``nameOrig`` → ``customer_id``, ``nameDest`` → ``beneficiary_id``;
* ``amount`` × ``paysim_try_per_unit`` → TRY amount (configured ratio);
* device, IP and session **do not exist** in PaySim and are left empty — never
  invented; the feature store turns them into missing indicators;
* ``isFlaggedFraud`` and the balance columns are **not** used as features
  (:data:`EXCLUDED_COLUMNS`): the simulator zeroes balances when fraud empties
  an account, so ``newbalanceOrig == 0`` / ``oldbalanceDest`` inconsistencies
  encode the label, and ``isFlaggedFraud`` is PaySim's own rule output.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import get_settings

#: PaySim ``type`` → (channel, purpose). ATM/cash rows use the cash channel.
TYPE_MAP: dict[str, tuple[str, str]] = {
    "PAYMENT": ("mobile", "Ödeme"),
    "TRANSFER": ("web", "Transfer"),
    "CASH_OUT": ("atm", "Nakit çekim"),
    "CASH_IN": ("atm", "Nakit yatırma"),
    "DEBIT": ("web", "Borç ödeme"),
}
#: never passed to the platform (label leakage — see module docstring)
EXCLUDED_COLUMNS = (
    "isFlaggedFraud",
    "oldbalanceOrg",
    "newbalanceOrig",
    "oldbalanceDest",
    "newbalanceDest",
)


@dataclass(frozen=True)
class PaySimFilter:
    """Memory-friendly subset selection for the 6.3 M-row file."""

    sample_frac: float = 1.0
    step_min: int = 1
    step_max: int = 10_000
    seed: int = 0

    def keep(self, row: dict[str, str]) -> bool:
        step = int(row["step"])
        if not self.step_min <= step <= self.step_max:
            return False
        if self.sample_frac >= 1.0:
            return True
        # sample whole *beneficiaries* so payee windows (fan-in, age) stay complete
        digest = hashlib.blake2b(f"{self.seed}:{row['nameDest']}".encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") / 2**64 < self.sample_frac


def read_rows(path: Path, flt: PaySimFilter | None = None) -> Iterator[dict[str, str]]:
    """Stream rows (the CSV is never fully loaded into memory)."""
    flt = flt or PaySimFilter()
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if flt.keep(row):
                yield row


def to_transaction(row: dict[str, str], index: int) -> dict[str, Any]:
    settings = get_settings()
    base = datetime.fromisoformat(settings.paysim_base_date)
    step = int(row["step"])
    channel, purpose = TYPE_MAP.get(row["type"], ("web", row["type"].title()))
    amount = round(float(row["amount"]) * settings.paysim_try_per_unit, 2)
    return {
        "transaction_id": f"PS-{index:08d}",
        "ts": (base + timedelta(hours=step, seconds=index % 3600)).isoformat(),
        "customer_id": row["nameOrig"],
        "amount": amount,
        "amount_try": amount,
        "currency": "TRY",
        "channel": channel,
        "purpose": purpose,
        "beneficiary_id": row["nameDest"],
        "device_id": "",
        "ip_address": "",
        "location": "",
        "country": "",
        "session": {},
        "label": int(row["isFraud"]),
        "typology": row["type"],
        "step": step,
    }


def load_transactions(
    path: Path, flt: PaySimFilter | None = None, *, limit: int | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, row in enumerate(read_rows(path, flt)):
        if limit is not None and i >= limit:
            break
        out.append(to_transaction(row, i))
    return out


def customer_records(
    transactions: list[dict[str, Any]], *, prior_amount_try: float
) -> list[dict[str, Any]]:
    """Minimal KYC records: PaySim has no customer attributes.

    Only the id is real; the amount prior is the *population* median of the
    training period (no per-customer fabrication), known devices/locations are
    empty.
    """
    seen = dict.fromkeys(t["customer_id"] for t in transactions)
    return [
        {"customer_id": cid, "avg_amount": prior_amount_try, "home_country": ""} for cid in seen
    ]
