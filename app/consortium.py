"""Consortium / federated signal simulation (P2.3, Feedzai IQ style).

Several banks share a **privacy-preserving deny-list**: identifiers (IBANs,
device fingerprints) are never exchanged in clear text, only as salted
SHA-256 digests (the salt is a consortium-wide secret, so outsiders cannot
brute-force the IBAN space). A payment to an IBAN — or from a device — that
*another* bank confirmed as fraud raises the ``consortium`` signal.

``federated_average`` is a dependency-free FedAvg demo: each bank trains a
logistic regression locally and only the weights are averaged (the optional
``flwr`` extra can replace it with a real Flower deployment).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.config import get_settings
from app.features.extractor import Extraction
from app.scoring.reasons import ReasonCode
from app.scoring.signals import SignalResult

OWN_BANK = "ANIL3"


def digest(kind: str, value: str, salt: str | None = None) -> str:
    salt = salt if salt is not None else get_settings().consortium_salt
    normalised = value.replace(" ", "").upper()
    return hashlib.sha256(f"{salt}|{kind}|{normalised}".encode()).hexdigest()


@dataclass
class ConsortiumHub:
    """In-process stand-in for the shared consortium service."""

    entries: dict[str, set[str]] = field(default_factory=dict)  # digest -> reporting banks
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def report(self, bank: str, kind: str, value: str) -> str:
        key = digest(kind, value)
        with self._lock:
            self.entries.setdefault(key, set()).add(bank)
        return key

    def report_digest(self, bank: str, key: str) -> None:
        with self._lock:
            self.entries.setdefault(key, set()).add(bank)

    def hits(self, kind: str, value: str, *, exclude: str = OWN_BANK) -> set[str]:
        if not value:
            return set()
        banks = self.entries.get(digest(kind, value), set())
        return {b for b in banks if b != exclude}

    def stats(self) -> dict[str, Any]:
        banks: dict[str, int] = {}
        for reporters in self.entries.values():
            for bank in reporters:
                banks[bank] = banks.get(bank, 0) + 1
        return {"entries": len(self.entries), "by_bank": dict(sorted(banks.items()))}


class ConsortiumSignal:
    name = "consortium"

    def __init__(self, hub: ConsortiumHub) -> None:
        self.hub = hub

    def evaluate(
        self, tx: dict[str, Any], extraction: Extraction, features: Mapping[str, float]
    ) -> SignalResult:
        iban_banks = self.hub.hits("iban", extraction.tx.beneficiary)
        device_banks = self.hub.hits("device", extraction.tx.device_id)
        banks = sorted(iban_banks | device_banks)
        score = 1.0 if iban_banks else 0.7 if device_banks else 0.0
        reasons = []
        if banks:
            what = "Alıcı IBAN" if iban_banks else "Cihaz"
            reasons.append(
                ReasonCode(
                    "CONSORTIUM_HIT",
                    f"{what} konsorsiyumda {len(banks)} başka bankada fraud olarak bildirilmiş",
                    "signal",
                    score,
                )
            )
        return SignalResult(
            score, reasons, {"banks": banks}, {"consortium_hit": 1.0 if banks else 0.0}
        )


def publish_fraud(hub: ConsortiumHub, beneficiaries: Iterable[str], devices: Iterable[str]) -> int:
    """Share our confirmed fraud identifiers (hashed) with the consortium."""
    n = 0
    for iban in beneficiaries:
        if iban:
            hub.report(OWN_BANK, "iban", iban)
            n += 1
    for device in devices:
        if device:
            hub.report(OWN_BANK, "device", device)
            n += 1
    return n


# --- federated learning demo (FedAvg) -------------------------------------------------------
def _local_logreg(x: np.ndarray, y: np.ndarray, w: np.ndarray, epochs: int, lr: float):
    for _ in range(epochs):
        p = 1.0 / (1.0 + np.exp(-(x @ w)))
        w = w - lr * (x.T @ (p - y)) / len(y)
    return w


def federated_average(
    banks: Sequence[tuple[np.ndarray, np.ndarray]],
    *,
    rounds: int = 20,
    epochs: int = 5,
    lr: float = 0.5,
) -> np.ndarray:
    """FedAvg over banks' local data: only weight vectors leave a bank."""
    dim = banks[0][0].shape[1] + 1
    weights = np.zeros(dim)
    sizes = np.array([len(y) for _, y in banks], dtype=float)
    for _ in range(rounds):
        local = []
        for x, y in banks:
            xb = np.hstack([x, np.ones((len(x), 1))])
            local.append(_local_logreg(xb, y, weights.copy(), epochs, lr))
        weights = np.average(np.stack(local), axis=0, weights=sizes)
    return weights
