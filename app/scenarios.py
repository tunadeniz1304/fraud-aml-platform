"""Attack scenario injector ("Demo modu", P2.1 #6) — also used by the
simulator and by the typology integration tests.

Each scenario builds a short, realistic sequence of transfers for customers of
the loaded population (a few days of normal *prelude* history, then the
attack) and names the transactions whose decision matters:

========== ================================================== =======================
scenario   pattern                                            expected action
========== ================================================== =======================
ato        new device + VPN/foreign IP + seconds after login,  BLOCK
           bot-like typing, drain to new IBANs
app        elderly victim on their *own* device, active call,  HOLD + dynamic warning
           "güvenli hesap" text, CoP mismatch, new IBAN
mule_ring  3 victims → mule (fan-in), mule forwards 90% from   HOLD + case + graph ring
           a device shared with another mule within minutes
smurfing   4 transfers just below the reporting threshold in   HOLD + AML case + ŞİB
           a few hours to new payees (business account)       (no block: tipping-off)
card_test  burst of tiny web payments from a bot device        BLOCK
========== ================================================== =======================
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import get_settings
from app.features.extractor import CustomerDirectory
from app.synthetic.generator import make_iban

SCENARIOS: dict[str, dict[str, str]] = {
    "ato": {"title": "Hesap ele geçirme (ATO)", "expected": "BLOCK"},
    "app": {"title": "APP dolandırıcılığı (güvenli hesap)", "expected": "HOLD"},
    "mule_ring": {"title": "Mule halkası (fan-in → fan-out)", "expected": "HOLD"},
    "smurfing": {"title": "Parçalama / smurfing (eşik altı)", "expected": "HOLD"},
    "card_testing": {"title": "Kart testi (küçük çoklu ödeme)", "expected": "BLOCK"},
}


class ScenarioError(ValueError):
    pass


@dataclass
class Scenario:
    name: str
    title: str
    expected: str
    transactions: list[dict[str, Any]]
    key_ids: list[str]
    customers: list[str] = field(default_factory=list)
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "expected": self.expected,
            "customers": self.customers,
            "transactions": len(self.transactions),
            "key_ids": self.key_ids,
            "notes": self.notes,
        }


class ScenarioFactory:
    def __init__(
        self,
        customers: CustomerDirectory,
        *,
        seed: int | None = None,
        now: datetime | None = None,
    ) -> None:
        self.records = customers.records()
        self.rng = random.Random(seed)
        self.now = (now or datetime.now()).replace(microsecond=0)
        self.run = uuid.uuid4().hex[:6].upper() if seed is None else f"S{seed}"
        self._seq = 0

    # --- helpers --------------------------------------------------------------------
    def _id(self, name: str) -> str:
        self._seq += 1
        return f"SCN-{name.upper()}-{self.run}-{self._seq:03d}"

    def _pick(
        self,
        *,
        exclude: frozenset[str] | set[str] = frozenset(),
        need_iban: bool = False,
        prefer_elderly: bool = False,
    ) -> dict[str, Any]:
        pool = [
            r
            for r in self.records
            if r["customer_id"] not in exclude and (r.get("iban") or not need_iban)
        ]
        if not pool:
            raise ScenarioError("senaryo için uygun müşteri yok (IBAN'lı nüfus gerekir)")
        if prefer_elderly:
            elderly = [
                r for r in pool if r.get("vulnerable") or (r.get("birth_year") or 3000) <= 1960
            ]
            pool = elderly or pool
        return self.rng.choice(pool)

    def _tx(self, c: dict[str, Any], ts: datetime, amount: float, name: str, **extra: Any):
        devices = c.get("known_device_ids") or ["DEV-UNKNOWN"]
        locations = c.get("known_locations") or ["İstanbul"]
        channels = c.get("known_channels") or ["mobile"]
        tx = {
            "transaction_id": self._id(name),
            "ts": ts.isoformat(timespec="seconds"),
            "customer_id": c["customer_id"],
            "amount": round(max(1.0, amount), 2),
            "currency": "TRY",
            "device_id": devices[0],
            "location": locations[0],
            "country": "TR",
            "channel": channels[0] if channels[0] != "atm" else "mobile",
            "ip_address": f"88.241.{self.rng.randint(1, 250)}.{self.rng.randint(1, 250)}",
            "purpose": "Market",
            "session": {
                "login_to_transfer_s": float(self.rng.randint(40, 200)),
                "session_duration_s": float(self.rng.randint(120, 400)),
                "typing_cadence_ms": float(c.get("typing_ms") or 220),
            },
        }
        tx.update(extra)
        return tx

    def _prelude(self, c: dict[str, Any], name: str, days: int = 3) -> list[dict[str, Any]]:
        """A few normal transfers on previous days (history + known payee)."""
        avg = float(c.get("avg_amount") or 1000)
        hours = c.get("typical_hours") or list(range(9, 19))
        regular = make_iban(self.rng)
        out = []
        for d in range(days, 0, -1):
            ts = (self.now - timedelta(days=d)).replace(hour=int(hours[len(hours) // 2]), minute=10)
            out.append(
                self._tx(
                    c,
                    ts,
                    avg * self.rng.uniform(0.6, 1.3),
                    name,
                    beneficiary_iban=regular,
                    beneficiary_name="Düzenli Alıcı",
                    purpose=self.rng.choice(("Kira", "Fatura", "Market")),
                )
            )
        return out

    # --- scenarios ------------------------------------------------------------------------
    def build(self, name: str, customer_id: str | None = None) -> Scenario:
        if name not in SCENARIOS:
            raise ScenarioError(f"bilinmeyen senaryo: {name}")
        builder = getattr(self, f"_{name}")
        scenario: Scenario = builder(customer_id)
        return scenario

    def _chosen(self, customer_id: str | None, **kw: Any) -> dict[str, Any]:
        if customer_id:
            for r in self.records:
                if r["customer_id"] == customer_id:
                    return r
            raise ScenarioError(f"müşteri bulunamadı: {customer_id}")
        return self._pick(**kw)

    def _ato(self, customer_id: str | None) -> Scenario:
        c = self._chosen(customer_id)
        txs = self._prelude(c, "ato")
        avg = float(c.get("avg_amount") or 1000)
        start = self.now - timedelta(minutes=6)
        device = f"DEV-{self.rng.getrandbits(32):08X}"
        keys = []
        for k in range(2):
            tx = self._tx(
                c,
                start + timedelta(minutes=2 * k),
                avg * self.rng.uniform(7, 12),
                "ato",
                device_id=device,
                ip_address=f"45.84.{self.rng.randint(1, 250)}.{self.rng.randint(1, 250)}",
                channel="web",
                purpose="",
                beneficiary_iban=make_iban(self.rng),
                beneficiary_name="Mehmet Kara",
                session={
                    "login_to_transfer_s": 9.0 + 20 * k,
                    "session_duration_s": 60.0 + 30 * k,
                    "typing_cadence_ms": round(float(c.get("typing_ms") or 220) * 0.35, 1),
                    "paste_used": True,
                    "is_vpn_or_tor": True,
                },
            )
            txs.append(tx)
            keys.append(tx["transaction_id"])
        meta = SCENARIOS["ato"]
        return Scenario("ato", meta["title"], meta["expected"], txs, keys, [c["customer_id"]])

    def _app(self, customer_id: str | None) -> Scenario:
        c = self._chosen(customer_id, prefer_elderly=True)
        others = [r for r in self.records if r["customer_id"] != c["customer_id"] and r.get("iban")]
        mule_iban = self.rng.choice(others)["iban"] if others else make_iban(self.rng)
        txs = self._prelude(c, "app")
        avg = float(c.get("avg_amount") or 1000)
        hours = c.get("typical_hours") or [14]
        ts = self.now.replace(hour=int(hours[len(hours) // 2]), minute=20)
        if ts > self.now:
            ts -= timedelta(days=1)
        tx = self._tx(
            c,
            ts,
            avg * self.rng.uniform(10, 16),
            "app",
            purpose="Polis talimatı ile güvenli hesaba aktarım",
            beneficiary_iban=mule_iban,
            beneficiary_name="Emniyet Güvenli Hesap",
            session={
                "login_to_transfer_s": 1500.0,
                "session_duration_s": 2100.0,
                "typing_cadence_ms": float(c.get("typing_ms") or 260),
                "active_call": True,
            },
        )
        txs.append(tx)
        meta = SCENARIOS["app"]
        return Scenario(
            "app", meta["title"], meta["expected"], txs, [tx["transaction_id"]], [c["customer_id"]]
        )

    def _mule_ring(self, customer_id: str | None) -> Scenario:
        mule = self._chosen(customer_id, need_iban=True)
        used = {mule["customer_id"]}
        mule2 = self._pick(exclude=used, need_iban=True)
        used.add(mule2["customer_id"])
        victims = []
        for _ in range(3):
            v = self._pick(exclude=used)
            used.add(v["customer_id"])
            victims.append(v)
        shared_device = f"DEV-{self.rng.getrandbits(32):08X}"
        cashout = make_iban(self.rng)
        start = self.now - timedelta(minutes=40)
        txs: list[dict[str, Any]] = []
        # the second mule already used the shared device earlier today
        txs.append(
            self._tx(
                mule2,
                start - timedelta(hours=3),
                float(mule2.get("avg_amount") or 1000),
                "mule",
                device_id=shared_device,
                beneficiary_iban=make_iban(self.rng),
                beneficiary_name="Nakit Noktası",
            )
        )
        received = 0.0
        for i, v in enumerate(victims):
            amount = self.rng.uniform(18_000, 35_000)
            received += amount
            txs.append(
                self._tx(
                    v,
                    start + timedelta(minutes=4 * i),
                    amount,
                    "mule",
                    beneficiary_iban=mule["iban"],
                    beneficiary_name=mule.get("name", ""),
                    purpose=self.rng.choice(("Kiralık ev kaporası", "Araç kaporası", "Ödeme")),
                )
            )
        fan_out = self._tx(
            mule,
            start + timedelta(minutes=22),
            received * 0.92,
            "mule",
            device_id=shared_device,
            beneficiary_iban=cashout,
            beneficiary_name="Nakit Noktası",
            purpose="Emanet",
        )
        txs.append(fan_out)
        meta = SCENARIOS["mule_ring"]
        return Scenario(
            "mule_ring",
            meta["title"],
            meta["expected"],
            txs,
            [fan_out["transaction_id"]],
            [mule["customer_id"], mule2["customer_id"], *(v["customer_id"] for v in victims)],
            notes=f"mule={mule['customer_id']} paylaşılan cihaz={shared_device}",
        )

    def _smurfing(self, customer_id: str | None) -> Scenario:
        if customer_id:
            c = self._chosen(customer_id)
        else:  # launderers use accounts with sizeable regular flows
            ranked = sorted(self.records, key=lambda r: -float(r.get("avg_amount") or 0))
            c = self.rng.choice(ranked[: max(1, len(ranked) // 10)])
        s = get_settings()
        txs = self._prelude(c, "smurf")
        start = self.now - timedelta(hours=4)
        keys = []
        for k in range(4):
            amount = s.structuring_threshold_try * self.rng.uniform(
                s.structuring_band + 0.01, 0.995
            )
            tx = self._tx(
                c,
                start + timedelta(minutes=55 * k),
                amount,
                "smurf",
                beneficiary_iban=make_iban(self.rng),
                beneficiary_name=f"Alıcı {k + 1}",
                purpose="Ödeme",
            )
            txs.append(tx)
            keys.append(tx["transaction_id"])
        meta = SCENARIOS["smurfing"]
        return Scenario(
            "smurfing", meta["title"], meta["expected"], txs, keys[2:], [c["customer_id"]]
        )

    def _card_testing(self, customer_id: str | None) -> Scenario:
        c = self._chosen(customer_id)
        txs = self._prelude(c, "card")
        start = self.now - timedelta(minutes=10)
        device = f"DEV-{self.rng.getrandbits(32):08X}"
        for k in range(8):
            txs.append(
                self._tx(
                    c,
                    start + timedelta(seconds=45 * k),
                    self.rng.uniform(1, 40),
                    "card",
                    device_id=device,
                    channel="web",
                    purpose="Online alışveriş",
                    beneficiary_iban=make_iban(self.rng),
                    beneficiary_name=f"Online Mağaza {k + 1:02d}",
                    session={"login_to_transfer_s": 4.0, "paste_used": True},
                )
            )
        meta = SCENARIOS["card_testing"]
        return Scenario(
            "card_testing",
            meta["title"],
            meta["expected"],
            txs,
            [txs[-1]["transaction_id"]],
            [c["customer_id"]],
        )
