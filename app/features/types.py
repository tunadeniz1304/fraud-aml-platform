"""Value types shared by the feature store, the extractor and training."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.fx import to_try
from app.core.signals import IPInfo


def _epoch(ts: datetime) -> float:
    return ts.timestamp()


@dataclass(frozen=True)
class TxView:
    """Normalised, immutable view of one incoming transaction."""

    transaction_id: str
    customer_id: str
    ts: datetime
    amount: float
    currency: str
    amount_try: float
    channel: str
    device_id: str
    ip_address: str
    location: str
    country: str
    beneficiary: str
    beneficiary_name: str
    purpose: str
    session: dict[str, Any] = field(default_factory=dict)

    @property
    def epoch(self) -> float:
        return _epoch(self.ts)

    @classmethod
    def from_payload(cls, tx: dict[str, Any]) -> TxView:
        ts = tx["ts"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        currency = str(tx.get("currency") or "TRY").upper()
        amount_try = tx.get("amount_try")
        if amount_try is None:
            amount_try = float(to_try(tx["amount"], currency))
        beneficiary = str(tx.get("beneficiary_iban") or tx.get("beneficiary_id") or "")
        session = tx.get("session") if isinstance(tx.get("session"), dict) else {}
        return cls(
            transaction_id=str(tx["transaction_id"]),
            customer_id=str(tx["customer_id"]),
            ts=ts,
            amount=float(tx["amount"]),
            currency=currency,
            amount_try=float(amount_try),
            channel=str(tx.get("channel") or ""),
            device_id=str(tx.get("device_id") or ""),
            ip_address=str(tx.get("ip_address") or ""),
            location=str(tx.get("location") or ""),
            country=str(tx.get("country") or "").upper(),
            beneficiary=beneficiary,
            beneficiary_name=str(tx.get("beneficiary_name") or ""),
            purpose=str(tx.get("purpose") or ""),
            session=dict(session or {}),
        )


@dataclass(frozen=True)
class HistEvent:
    """Compact past event kept in the customer's sliding window."""

    ts: float
    amount: float
    beneficiary: str
    channel: str
    device: str
    hour: int

    def as_list(self) -> list[Any]:
        return [self.ts, self.amount, self.beneficiary, self.channel, self.device, self.hour]

    @classmethod
    def from_list(cls, row: list[Any]) -> HistEvent:
        return cls(float(row[0]), float(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]))


@dataclass(frozen=True)
class CustomerStatic:
    """KYC / onboarding data (does not change with transactions)."""

    customer_id: str
    name: str = ""
    avg_amount_try: float = 1000.0
    known_devices: frozenset[str] = frozenset()
    known_locations: frozenset[str] = frozenset()
    typical_hours: frozenset[int] = frozenset()
    known_channels: frozenset[str] = frozenset()
    home_country: str = "TR"
    birth_year: int | None = None
    vulnerable: bool = False
    iban: str = ""
    typing_ms: float | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CustomerStatic:
        avg = float(to_try(record.get("avg_amount", 1000), record.get("currency", "TRY")))
        return cls(
            customer_id=str(record["customer_id"]),
            name=str(record.get("name", "")),
            avg_amount_try=max(1.0, avg),
            known_devices=frozenset(record.get("known_device_ids") or []),
            known_locations=frozenset(record.get("known_locations") or []),
            typical_hours=frozenset(int(h) for h in record.get("typical_hours") or []),
            known_channels=frozenset(record.get("known_channels") or []),
            home_country=str(record.get("home_country") or "TR"),
            birth_year=record.get("birth_year"),
            vulnerable=bool(record.get("vulnerable", False)),
            iban=str(record.get("iban") or ""),
            typing_ms=record.get("typing_ms"),
        )


@dataclass
class ProfileState:
    """Adaptive (EWMA) behavioural profile of one customer (ARIC-style ABA)."""

    n: float = 0.0
    log_mean: float = 0.0
    log_var: float = 0.5
    max_amount: float = 0.0
    hour_hist: list[float] = field(default_factory=lambda: [0.0] * 24)
    channels: dict[str, float] = field(default_factory=dict)
    devices: dict[str, float] = field(default_factory=dict)  # device -> last seen epoch
    typing_mean: float = 0.0
    typing_var: float = 0.0
    typing_n: float = 0.0

    @classmethod
    def from_static(cls, customer: CustomerStatic, prior_weight: float) -> ProfileState:
        hours = customer.typical_hours or frozenset(range(9, 19))
        hist = [0.0] * 24
        for h in hours:
            hist[h % 24] = prior_weight / len(hours)
        channels = {
            c: prior_weight / max(1, len(customer.known_channels)) for c in customer.known_channels
        }
        state = cls(
            n=prior_weight,
            log_mean=math.log1p(customer.avg_amount_try),
            log_var=0.5,
            max_amount=customer.avg_amount_try * 2.0,
            hour_hist=hist,
            channels=channels,
            devices={d: 0.0 for d in customer.known_devices},
        )
        if customer.typing_ms:
            state.typing_mean = float(customer.typing_ms)
            state.typing_var = (0.2 * float(customer.typing_ms)) ** 2
            state.typing_n = prior_weight
        return state

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "log_mean": self.log_mean,
            "log_var": self.log_var,
            "max_amount": self.max_amount,
            "hour_hist": self.hour_hist,
            "channels": self.channels,
            "devices": self.devices,
            "typing_mean": self.typing_mean,
            "typing_var": self.typing_var,
            "typing_n": self.typing_n,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileState:
        return cls(**data)

    def copy(self) -> ProfileState:
        return ProfileState.from_dict(
            {
                **self.to_dict(),
                "hour_hist": list(self.hour_hist),
                "channels": dict(self.channels),
                "devices": dict(self.devices),
            }
        )


@dataclass(frozen=True)
class StateSnapshot:
    """Point-in-time state read from the store *before* the current event.

    ``history`` is sorted by timestamp (both backends guarantee it).
    """

    history: tuple[HistEvent, ...] = ()
    pair_first_seen: float | None = None
    payee_first_seen: float | None = None
    payee_senders_24h: frozenset[str] = frozenset()
    device_first_seen: float | None = None
    device_customers_7d: frozenset[str] = frozenset()
    profile: ProfileState | None = None


@dataclass(frozen=True)
class FeatureContext:
    tx: TxView
    customer: CustomerStatic
    state: StateSnapshot
    profile: ProfileState
    ip: IPInfo | None
    extras: dict[str, float] = field(default_factory=dict)
    #: per-extraction memo (window slices etc.) — never persisted
    cache: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
