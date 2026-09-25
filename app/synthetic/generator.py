"""Seeded synthetic Turkish-banking transaction generator (P0.7).

Produces customers, a payee registry and a labelled, time-ordered stream of
outgoing transfers. Normal behaviour follows each customer's own habits
(regular payees, usual hours/channels/devices, salary-day peaks, low night
volume); labelled fraud typologies are injected on top:

* ``ato`` — account takeover: new device + new/anonymous network + fast
  login-to-transfer + bot-like typing + rapid drain to mule accounts;
* ``app`` — authorised push payment scam: victims skew elderly/vulnerable,
  *known* device, active call / remote access, urgency text, long session,
  new payee whose registered name does not match the typed name (CoP);
* ``mule`` — mule rings: shared devices, fan-in from victims then fan-out to
  cash-out accounts within 30 minutes, occasional layering cycles;
* ``card_testing`` — bursts of tiny payments to many merchants;
* ``structuring`` — smurfing just below the reporting threshold;
* ``sanctions`` — payments to listed names (excluded from ML training: the
  sanctions screener, not the model, owns these).

Everything derives from one :class:`random.Random` seed, so the same seed
always yields byte-identical output. Names/IBANs are fictional.

**No fingerprints (v2).** Device ids come from one typology-independent
format (:meth:`_Builder._device`), legitimate traffic changes devices at a
realistic rate, part of the account takeovers run from the victim's *known*
device (session hijack / remote access), mule rings do not always share a
device and a configurable share of labels is flipped (label noise). A leakage
detector test checks that identifier fields alone cannot predict fraud.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import get_settings

FIRST_NAMES = [
    "Ayşe",
    "Fatma",
    "Emine",
    "Hatice",
    "Zeynep",
    "Elif",
    "Meryem",
    "Şerife",
    "Sultan",
    "Zehra",
    "Hülya",
    "Derya",
    "Esra",
    "Gamze",
    "Merve",
    "Büşra",
    "Ebru",
    "Özlem",
    "Sevgi",
    "Tuğba",
    "Selin",
    "Ece",
    "Defne",
    "Yasemin",
    "Nur",
    "Mehmet",
    "Mustafa",
    "Ahmet",
    "Ali",
    "Hüseyin",
    "İbrahim",
    "İsmail",
    "Osman",
    "Yusuf",
    "Murat",
    "Ömer",
    "Ramazan",
    "Halil",
    "Süleyman",
    "Abdullah",
    "Mahmut",
    "Emre",
    "Burak",
    "Serkan",
    "Onur",
    "Kemal",
    "Cem",
    "Barış",
    "Tolga",
    "Kaan",
    "Deniz",
    "Umut",
    "Volkan",
    "Tuncay",
    "Levent",
]
LAST_NAMES = [
    "Yılmaz",
    "Kaya",
    "Demir",
    "Şahin",
    "Çelik",
    "Yıldız",
    "Yıldırım",
    "Öztürk",
    "Aydın",
    "Özdemir",
    "Arslan",
    "Doğan",
    "Kılıç",
    "Aslan",
    "Çetin",
    "Kara",
    "Koç",
    "Kurt",
    "Özkan",
    "Şimşek",
    "Polat",
    "Korkmaz",
    "Erdoğan",
    "Güneş",
    "Aksoy",
    "Tekin",
    "Avcı",
    "Bulut",
    "Keskin",
    "Uçar",
    "Taş",
    "Acar",
    "Güler",
    "Ünal",
    "Sarı",
    "Toprak",
    "Işık",
    "Erdem",
    "Bozkurt",
    "Coşkun",
]
CITIES = [
    "İstanbul",
    "İstanbul",
    "İstanbul",
    "Ankara",
    "Ankara",
    "İzmir",
    "İzmir",
    "Bursa",
    "Antalya",
    "Adana",
    "Konya",
    "Gaziantep",
    "Kocaeli",
    "Mersin",
    "Kayseri",
    "Eskişehir",
    "Samsun",
    "Trabzon",
    "Denizli",
    "Diyarbakır",
]
NORMAL_PURPOSES = (
    "Kira",
    "Market",
    "Fatura",
    "Aile desteği",
    "Aidat",
    "Eğitim",
    "Alışveriş",
    "Borç ödemesi",
    "Harçlık",
    "Yemek",
    "Sağlık",
    "Tatil",
    "",
)
URGENT_PURPOSES = (
    "Güvenli hesaba aktarım",
    "ACİL vergi borcu ödemesi",
    "Polis talimatı ile güvenli hesaba",
    "Yatırım - garantili kazanç",
    "Kripto yatırım acil",
    "Hesabınız tehlikede, hemen aktarın",
    "Savcılık dosyası teminatı",
)
BILLERS = (
    "Elektrik Dağıtım AŞ",
    "Doğalgaz Dağıtım AŞ",
    "Su ve Kanalizasyon İdaresi",
    "Mobil Operatör AŞ",
    "İnternet Servis AŞ",
    "Site Yönetimi",
    "Özel Okul AŞ",
    "Sigorta AŞ",
)
BIG_PURCHASES = (
    "Araç kaporası",
    "Kira depozitosu",
    "Tapu masrafı",
    "Düğün",
    "Mobilya",
    "Ödeme",
)
MERCHANTS = tuple(f"Online Mağaza {i:02d}" for i in range(1, 41))
DOMESTIC_IP_PREFIXES = ("88.241", "5.176", "176.40", "176.41", "78.161", "78.162", "31.140")
FOREIGN = (("Berlin", "DE", "89.12"), ("Dubai", "AE", "92.96"), ("Lagos", "NG", "197.210"))
ANON_IP_PREFIXES = ("45.84", "104.244.73", "185.220.101", "193.29.61")
BANK_CODES = ("00010", "00012", "00015", "00046", "00062", "00064", "00067", "00111")


@dataclass
class SyntheticConfig:
    seed: int = 42
    customers: int = 500
    days: int = 60
    start: datetime = datetime(2026, 6, 1)
    mean_daily_tx: float = 1.8
    ato_attacks: int = 45
    app_scams: int = 55
    mule_rings: int = 4
    ring_size: int = 5
    card_testing: int = 25
    structuring: int = 22
    sanctions_hits: int = 10
    #: one-off new device of a legitimate customer (borrowed phone, new browser)
    legit_new_device_rate: float = 0.05
    #: per-day probability that a customer permanently switches phones
    phone_change_rate: float = 0.004
    #: account takeovers performed from the victim's own device (hijack / RAT)
    ato_known_device_rate: float = 0.35
    #: mule transfers sent from a device shared inside the ring
    mule_shared_device_rate: float = 0.35
    #: card-testing bursts run from an already known device (infected browser)
    card_known_device_rate: float = 0.25
    #: share of labels flipped (missed chargebacks / friendly-fraud disputes)
    label_noise_rate: float = 0.01
    legit_new_payee_rate: float = 0.07
    legit_travel_rate: float = 0.006
    legit_vpn_rate: float = 0.006


@dataclass
class SyntheticDataset:
    config: SyntheticConfig
    customers: list[dict[str, Any]]
    transactions: list[dict[str, Any]]
    payees: list[dict[str, Any]]
    roles: dict[str, str] = field(default_factory=dict)
    rings: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        typ = Counter(t["typology"] for t in self.transactions)
        frauds = sum(t["label"] for t in self.transactions)
        return {
            "seed": self.config.seed,
            "customers": len(self.customers),
            "transactions": len(self.transactions),
            "fraud": frauds,
            "fraud_rate": round(frauds / max(1, len(self.transactions)), 5),
            "typologies": dict(sorted(typ.items())),
            "payees": len(self.payees),
            "rings": len(self.rings),
        }

    def write(self, out_dir: Path) -> dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "customers": out_dir / "customers.json",
            "transactions": out_dir / "transactions.jsonl",
            "payees": out_dir / "payees.json",
            "manifest": out_dir / "manifest.json",
        }
        paths["customers"].write_text(
            json.dumps(self.customers, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        with paths["transactions"].open("w", encoding="utf-8", newline="\n") as fh:
            for tx in self.transactions:
                fh.write(json.dumps(tx, ensure_ascii=False, sort_keys=True) + "\n")
        paths["payees"].write_text(
            json.dumps(self.payees, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        manifest = {**self.summary(), "rings": self.rings}
        paths["manifest"].write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return paths


def load_dataset(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read ``customers.json`` + ``transactions.jsonl`` written by :meth:`write`."""
    customers = json.loads((directory / "customers.json").read_text(encoding="utf-8"))
    with (directory / "transactions.jsonl").open("r", encoding="utf-8") as fh:
        transactions = [json.loads(line) for line in fh if line.strip()]
    return customers, transactions


def iban_check(bban: str, country: str = "TR") -> str:
    """ISO 13616 mod-97 check digits."""
    rearranged = bban + country + "00"
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return f"{98 - int(digits) % 97:02d}"


def make_iban(rng: random.Random) -> str:
    bban = rng.choice(BANK_CODES) + "0" + "".join(str(rng.randint(0, 9)) for _ in range(16))
    return f"TR{iban_check(bban)}{bban}"


class _Builder:
    def __init__(self, cfg: SyntheticConfig) -> None:
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.tx: list[dict[str, Any]] = []
        self.customers: list[dict[str, Any]] = []
        self.payees: dict[str, dict[str, Any]] = {}
        self.regular: dict[str, list[str]] = {}
        self.roles: dict[str, str] = {}
        self.rings: list[dict[str, Any]] = []
        self._used_names: set[str] = set()
        s = get_settings()
        self.threshold = s.structuring_threshold_try
        self.band = s.structuring_band

    # --- primitives ---------------------------------------------------------------
    def _name(self) -> str:
        rng = self.rng
        for _ in range(50):
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in self._used_names:
                self._used_names.add(name)
                return name
        return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)} {rng.randint(2, 99)}"

    def _device(self) -> str:
        """Typology-independent device id (no ``DEV-ATO-*`` style prefixes)."""
        return f"DEV-{self.rng.getrandbits(32):08X}"

    def _ip(self, prefix: str) -> str:
        parts = prefix.split(".")
        while len(parts) < 4:
            parts.append(str(self.rng.randint(1, 250)))
        return ".".join(parts)

    def _payee(
        self,
        name: str,
        *,
        kind: str,
        owner: str | None = None,
        opened: datetime | None = None,
        mule: bool = False,
        iban: str | None = None,
    ) -> str:
        iban = iban or make_iban(self.rng)
        opened = opened or self.cfg.start - timedelta(days=self.rng.randint(400, 5000))
        self.payees[iban] = {
            "iban": iban,
            "name": name,
            "kind": kind,
            "owner_customer_id": owner,
            "account_opened": opened.date().isoformat(),
            "is_mule": mule,
        }
        return iban

    def _session(self, c: dict[str, Any], **over: Any) -> dict[str, Any]:
        rng = self.rng
        ltt = max(3.0, rng.lognormvariate(math.log(70), 0.9))
        session = {
            "login_to_transfer_s": round(ltt, 1),
            "session_duration_s": round(ltt + rng.lognormvariate(math.log(60), 0.5), 1),
            "typing_cadence_ms": round(
                max(40.0, rng.gauss(c["typing_ms"], 0.12 * c["typing_ms"])), 1
            ),
            "paste_used": rng.random() < 0.03,
            "active_call": rng.random() < 0.004,
            "remote_access_tool": rng.random() < 0.001,
            "is_emulator": False,
            "is_rooted": rng.random() < 0.002,
        }
        session.update(over)
        return session

    def _base_tx(self, c: dict[str, Any], ts: datetime, amount: float) -> dict[str, Any]:
        rng = self.rng
        return {
            "ts": ts.isoformat(timespec="seconds"),
            "customer_id": c["customer_id"],
            "amount": round(max(1.0, amount), 2),
            "currency": "TRY",
            "device_id": rng.choice(c["_devices"]),
            "location": rng.choice(c["known_locations"]),
            "country": "TR",
            "channel": rng.choice(c["known_channels"]),
            "ip_address": self._ip(c["_ip_prefix"]),
            "beneficiary_id": "",
            "beneficiary_iban": "",
            "beneficiary_name": "",
            "purpose": rng.choice(NORMAL_PURPOSES),
            # ~15% of feeds carry no device/session block (ATM, branch, legacy channels)
            "session": self._session(c) if rng.random() >= 0.15 else {},
            "label": 0,
            "typology": "normal",
        }

    def _to(self, tx: dict[str, Any], iban: str, typed_name: str | None = None) -> dict[str, Any]:
        tx["beneficiary_iban"] = iban
        tx["beneficiary_id"] = iban
        tx["beneficiary_name"] = typed_name if typed_name is not None else self.payees[iban]["name"]
        return tx

    def _emit(self, tx: dict[str, Any]) -> dict[str, Any]:
        self.tx.append(tx)
        return tx

    def _hour(self, c: dict[str, Any]) -> int:
        rng = self.rng
        if rng.random() < 0.93:
            return rng.choice(c["typical_hours"])
        # diurnal fallback: night hours are rare
        weights = [0.2 if h < 6 else (1.0 if h < 23 else 0.5) for h in range(24)]
        return rng.choices(range(24), weights=weights)[0]

    def _time(self, day: int, hour: int) -> datetime:
        return self.cfg.start + timedelta(
            days=day, hours=hour, minutes=self.rng.randint(0, 59), seconds=self.rng.randint(0, 59)
        )

    # --- population -----------------------------------------------------------------
    def build_customers(self) -> None:
        rng = self.rng
        for i in range(self.cfg.customers):
            cid = f"CUST-S{i + 1:04d}"
            name = self._name()
            elderly = rng.random() < 0.14
            birth_year = rng.randint(1935, 1960) if elderly else rng.randint(1962, 2004)
            start_hour = rng.randint(7, 11)
            end_hour = min(23, start_hour + rng.randint(8, 13))
            hours = list(range(start_hour, end_hour + 1))
            if rng.random() < 0.05:  # gece vardiyası çalışanları
                hours = [*range(20, 24), *range(0, 5)]
            channels = rng.choice((["mobile"], ["mobile", "web"], ["web"], ["mobile", "web"]))
            if rng.random() < 0.15:
                channels = [*channels, "atm"]
            home = rng.choice(CITIES)
            locations = [home] + ([rng.choice(CITIES)] if rng.random() < 0.4 else [])
            segment = rng.choices(("bireysel", "premium", "kobi"), weights=(0.8, 0.12, 0.08))[0]
            base = {"bireysel": 1200.0, "premium": 6000.0, "kobi": 9000.0}[segment]
            avg = round(base * rng.lognormvariate(0, 0.55), 0)
            c: dict[str, Any] = {
                "customer_id": cid,
                "name": name,
                "home_city": home,
                "home_country": "TR",
                "known_device_ids": [self._device() for _ in range(rng.choice((1, 1, 2, 2, 3)))],
                "known_locations": list(dict.fromkeys(locations)),
                "avg_amount": avg,
                "typical_hours": hours,
                "currency": "TRY",
                "known_channels": channels,
                "birth_year": birth_year,
                "vulnerable": elderly and rng.random() < 0.5,
                "segment": segment,
                "typing_ms": round(rng.uniform(140, 320) * (1.35 if elderly else 1.0), 1),
                "_ip_prefix": rng.choice(DOMESTIC_IP_PREFIXES),
                "_rate": rng.lognormvariate(math.log(self.cfg.mean_daily_tx), 0.45),
            }
            c["_devices"] = list(c["known_device_ids"])
            c["iban"] = self._payee(name, kind="person", owner=cid)
            self.customers.append(c)
            self.roles[cid] = "normal"
        for c in self.customers:
            others = [o for o in self.customers if o is not c]
            payees = [o["iban"] for o in rng.sample(others, rng.randint(2, 5))]
            for _ in range(rng.randint(1, 3)):
                biller = rng.choice(BILLERS)
                payees.append(self._payee(biller, kind="biller"))
            self.regular[c["customer_id"]] = payees
        # family members sharing a tablet (legitimate shared device)
        for _ in range(self.cfg.customers // 30):
            a, b = rng.sample(self.customers, 2)
            shared = self._device()
            for c in (a, b):
                c["known_device_ids"] = [*c["known_device_ids"], shared]
                c["_devices"] = [*c["_devices"], shared]

    def assign_mule_rings(self) -> None:
        rng = self.rng
        pool = [c for c in self.customers if c["birth_year"] > 1975]
        chosen = rng.sample(pool, self.cfg.mule_rings * self.cfg.ring_size)
        for r in range(self.cfg.mule_rings):
            members = chosen[r * self.cfg.ring_size : (r + 1) * self.cfg.ring_size]
            devices = [self._device() for _ in range(rng.randint(1, 2))]
            cashouts = [
                self._payee(
                    self._name(),
                    kind="cashout",
                    mule=True,
                    opened=self.cfg.start - timedelta(days=rng.randint(5, 60)),
                )
                for _ in range(2)
            ]
            for m in members:
                self.roles[m["customer_id"]] = "mule"
                self.payees[m["iban"]]["is_mule"] = True
                self.payees[m["iban"]]["account_opened"] = (
                    (self.cfg.start - timedelta(days=rng.randint(10, 90))).date().isoformat()
                )
            self.rings.append(
                {
                    "ring": r + 1,
                    "members": [m["customer_id"] for m in members],
                    "devices": devices,
                    "cashouts": cashouts,
                }
            )

    # --- normal behaviour ------------------------------------------------------------
    def normal_activity(self) -> None:
        rng = self.rng
        for c in self.customers:
            for day in range(self.cfg.days):
                date = self.cfg.start + timedelta(days=day)
                rate = c["_rate"] * (0.7 if date.weekday() >= 5 else 1.0)
                if date.day in (1, 15):  # maaş günleri
                    rate += 1.0
                if rng.random() < self.cfg.phone_change_rate:  # telefon değişikliği (kalıcı)
                    c["_devices"] = [self._device()]
                for _ in range(_poisson(rng, rate)):
                    self._normal_tx(c, self._time(day, self._hour(c)))
                if rng.random() < 0.03:  # aynı oturumda art arda fatura ödemeleri
                    ts = self._time(day, self._hour(c))
                    for _ in range(rng.randint(2, 4)):
                        ts += timedelta(seconds=rng.uniform(20, 180))
                        self._normal_tx(c, ts, bill=True)
            del c["_rate"]

    def _normal_tx(self, c: dict[str, Any], ts: datetime, *, bill: bool = False) -> None:
        rng = self.rng
        amount = c["avg_amount"] * rng.lognormvariate(0, 0.6) * (0.3 if bill else 1.0)
        tx = self._base_tx(c, ts, amount)
        roll = rng.random()
        elderly = c["birth_year"] <= 1960
        if elderly and rng.random() < 0.03:  # aileyle telefonda konuşurken ödeme
            tx["session"]["active_call"] = True
        if bill:
            tx["purpose"] = "Fatura"
            self._to(tx, rng.choice(self.regular[c["customer_id"]]))
        elif roll < 0.004:  # meşru büyük alım: yeni alıcıya yüksek tutar
            tx["amount"] = round(c["avg_amount"] * rng.uniform(5, 25), 2)
            tx["purpose"] = rng.choice(BIG_PURCHASES)
            self._to(tx, self._payee(self._name(), kind="person"))
        elif c["segment"] == "kobi" and roll < 0.04:  # tedarikçi ödemesi (eşik altı, meşru)
            tx["amount"] = round(self.threshold * rng.uniform(self.band, 0.999), 2)
            tx["purpose"] = "Tedarikçi ödemesi"
            self._to(tx, rng.choice(self.regular[c["customer_id"]]))
        elif roll < self.cfg.legit_new_payee_rate:
            iban = self._payee(self._name(), kind="person")
            self.regular[c["customer_id"]].append(iban)
            self._to(tx, iban)
        elif tx["channel"] == "atm":
            tx["purpose"] = "Nakit çekim"
            tx["session"] = {}
        else:
            regular = self.regular[c["customer_id"]]
            weights = [1.0 / (i + 1) for i in range(len(regular))]
            self._to(tx, rng.choices(regular, weights=weights)[0])
        if rng.random() < self.cfg.legit_new_device_rate:
            # one-off legitimate new device (e.g. a borrowed phone) — never added to KYC
            tx["device_id"] = self._device()
        if rng.random() < self.cfg.legit_travel_rate:
            city, country, prefix = rng.choice(FOREIGN[:2])
            tx.update(location=city, country=country, ip_address=self._ip(prefix))
        elif rng.random() < self.cfg.legit_vpn_rate:
            tx["ip_address"] = self._ip(rng.choice(ANON_IP_PREFIXES))
        self._emit(tx)

    # --- typologies ---------------------------------------------------------------------
    def _victim(self, *, prefer_elderly: bool = False) -> dict[str, Any]:
        rng = self.rng
        pool = [c for c in self.customers if self.roles[c["customer_id"]] == "normal"]
        if prefer_elderly and rng.random() < 0.6:
            elderly = [c for c in pool if c["birth_year"] <= 1960 or c["vulnerable"]]
            pool = elderly or pool
        return rng.choice(pool)

    def _mule_iban(self) -> tuple[str, dict[str, Any]]:
        ring = self.rng.choice(self.rings)
        member = self.rng.choice(ring["members"])
        customer = next(c for c in self.customers if c["customer_id"] == member)
        return customer["iban"], ring

    def _fan_out(self, ring: dict[str, Any], mule_iban: str, received: float, ts: datetime) -> None:
        """Mule forwards the money within ~30 minutes from a shared device."""
        rng = self.rng
        member = next(c for c in self.customers if c["iban"] == mule_iban)
        remaining = received * rng.uniform(0.85, 0.97)
        parts = rng.randint(1, 3)
        t = ts
        for k in range(parts):
            t = t + timedelta(minutes=rng.uniform(3, 28 / parts))
            amount = remaining / (parts - k) * rng.uniform(0.9, 1.1) if k < parts - 1 else remaining
            remaining -= amount
            if rng.random() < 0.7:
                target = rng.choice(ring["cashouts"])
            else:
                peer = rng.choice([m for m in ring["members"] if m != member["customer_id"]])
                target = next(c for c in self.customers if c["customer_id"] == peer)["iban"]
            tx = self._base_tx(member, t, amount)
            tx.update(
                device_id=rng.choice(ring["devices"])
                if rng.random() < self.cfg.mule_shared_device_rate
                else tx["device_id"],
                purpose=rng.choice(("", "Borç", "Ödeme", "Emanet")),
                label=1,
                typology="mule",
                session=self._session(member, login_to_transfer_s=round(rng.uniform(15, 60), 1)),
            )
            self._emit(self._to(tx, target))

    def ato(self) -> None:
        rng = self.rng
        for _ in range(self.cfg.ato_attacks):
            c = self._victim()
            day = rng.randint(8, self.cfg.days - 1)
            ts = self._time(day, rng.randint(0, 23))
            # oturum çalma / uzaktan erişim: saldırı kurbanın bilinen cihazından gelir
            rat = rng.random() < self.cfg.ato_known_device_rate
            device = rng.choice(c["_devices"]) if rat else self._device()
            roll = rng.random()
            if rat:
                ip, country, city = self._ip(c["_ip_prefix"]), "TR", c["home_city"]
            elif roll < 0.35:
                ip, country, city = self._ip(rng.choice(ANON_IP_PREFIXES)), "TR", c["home_city"]
            elif roll < 0.55:
                city, country, prefix = rng.choice(FOREIGN)
                ip = self._ip(prefix)
            else:
                ip, country, city = (
                    self._ip(rng.choice(DOMESTIC_IP_PREFIXES)),
                    "TR",
                    rng.choice(CITIES),
                )
            bot = rng.random() < 0.45
            cadence = c["typing_ms"] * (
                rng.choice((rng.uniform(0.25, 0.5), rng.uniform(1.8, 2.6)))
                if bot
                else rng.uniform(0.85, 1.2)
            )
            fast = rng.random() < 0.45
            balance = c["avg_amount"] * rng.uniform(2, 20)
            for k in range(rng.randint(1, 4)):
                ts = ts + timedelta(minutes=rng.uniform(0.5, 12))
                amount = balance * rng.uniform(0.3, 0.6)
                balance -= amount
                mule_iban, ring = self._mule_iban()
                tx = self._base_tx(c, ts, amount)
                tx.update(
                    device_id=device,
                    ip_address=ip,
                    country=country,
                    location=city,
                    channel=rng.choice(("web", "mobile")),
                    purpose=rng.choice(("", "Ödeme", "Borç")),
                    label=1,
                    typology="ato",
                    session=self._session(
                        c,
                        login_to_transfer_s=round(
                            (rng.uniform(4, 28) if fast else rng.uniform(30, 240)) + 20 * k, 1
                        ),
                        typing_cadence_ms=round(cadence, 1),
                        paste_used=rng.random() < 0.6,
                        is_emulator=rng.random() < 0.1,
                        remote_access_tool=rat and rng.random() < 0.5,
                    ),
                )
                self._emit(self._to(tx, mule_iban))
                self._fan_out(ring, mule_iban, amount, ts)

    def app(self) -> None:
        rng = self.rng
        for _ in range(self.cfg.app_scams):
            c = self._victim(prefer_elderly=True)
            day = rng.randint(8, self.cfg.days - 1)
            ts = self._time(day, rng.choice(c["typical_hours"]))
            mule_iban, ring = self._mule_iban()
            typed = rng.choice(
                (
                    "Emniyet Güvenli Hesap",
                    "Vergi Dairesi Tahsilat",
                    "Yatırım Danışmanı",
                    self._name(),
                )
            )
            call = rng.random() < 0.55
            remote = (not call and rng.random() < 0.45) or rng.random() < 0.1
            for k in range(rng.choice((1, 1, 2))):
                ts = ts + timedelta(minutes=rng.uniform(2, 40) * k)
                tx = self._base_tx(c, ts, c["avg_amount"] * rng.uniform(2, 30))
                ltt = rng.uniform(300, 1800)
                tx.update(
                    purpose=rng.choice(URGENT_PURPOSES)
                    if rng.random() < 0.6
                    else rng.choice(("Ödeme", "Borç", "Kira")),
                    label=1,
                    typology="app",
                    session=self._session(
                        c,
                        active_call=call,
                        remote_access_tool=remote,
                        login_to_transfer_s=round(ltt, 1),
                        session_duration_s=round(ltt + rng.uniform(120, 1800), 1),
                    ),
                )
                self._emit(self._to(tx, mule_iban, typed_name=typed))
                self._fan_out(ring, mule_iban, tx["amount"], ts)

    def layering_cycles(self) -> None:
        """A → B → C → A within a ring (katmanlama)."""
        rng = self.rng
        for ring in self.rings:
            members = [c for c in self.customers if c["customer_id"] in ring["members"]]
            for _ in range(4):
                day = rng.randint(5, self.cfg.days - 1)
                ts = self._time(day, rng.randint(9, 22))
                amount = rng.uniform(15_000, 60_000)
                cycle = rng.sample(members, 3)
                for i, src in enumerate(cycle):
                    dst = cycle[(i + 1) % 3]
                    ts = ts + timedelta(minutes=rng.uniform(5, 25))
                    amount *= rng.uniform(0.95, 0.99)
                    tx = self._base_tx(src, ts, amount)
                    tx.update(
                        device_id=rng.choice(ring["devices"])
                        if rng.random() < self.cfg.mule_shared_device_rate
                        else tx["device_id"],
                        purpose="Emanet",
                        label=1,
                        typology="mule",
                    )
                    self._emit(self._to(tx, dst["iban"]))

    def card_testing(self) -> None:
        rng = self.rng
        for _ in range(self.cfg.card_testing):
            c = self._victim()
            ts = self._time(rng.randint(3, self.cfg.days - 1), rng.randint(0, 23))
            known = rng.random() < self.cfg.card_known_device_rate
            device = rng.choice(c["_devices"]) if known else self._device()
            ip = self._ip(rng.choice(ANON_IP_PREFIXES + DOMESTIC_IP_PREFIXES * 2))
            for _ in range(rng.randint(6, 16)):
                ts = ts + timedelta(seconds=rng.uniform(10, 90))
                merchant = self._payee(rng.choice(MERCHANTS), kind="merchant")
                tx = self._base_tx(c, ts, rng.uniform(1, 90))
                tx.update(
                    device_id=device,
                    ip_address=ip,
                    channel="web",
                    purpose="Online alışveriş",
                    label=1,
                    typology="card_testing",
                    session=self._session(
                        c, login_to_transfer_s=round(rng.uniform(2, 10), 1), paste_used=True
                    ),
                )
                self._emit(self._to(tx, merchant))

    def structuring(self) -> None:
        rng = self.rng
        launderers = [c for c in self.customers if self.roles[c["customer_id"]] == "mule"]
        launderers += rng.sample(
            [c for c in self.customers if self.roles[c["customer_id"]] == "normal"], 6
        )
        for _ in range(self.cfg.structuring):
            c = rng.choice(launderers)
            ts = self._time(rng.randint(3, self.cfg.days - 1), rng.randint(9, 20))
            for _ in range(rng.randint(3, 6)):
                ts = ts + timedelta(minutes=rng.uniform(20, 240))
                amount = self.threshold * rng.uniform(self.band + 0.005, 0.998)
                target = self._payee(self._name(), kind="person")
                tx = self._base_tx(c, ts, amount)
                tx.update(
                    purpose=rng.choice(("", "Ödeme", "Borç")), label=1, typology="structuring"
                )
                self._emit(self._to(tx, target))

    def sanctions(self) -> None:
        rng = self.rng
        path = get_settings().data_dir / "sanctions.json"
        records = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        persons = [r for r in records if len(r["name"].split()) >= 2]
        for _ in range(self.cfg.sanctions_hits if persons else 0):
            record = rng.choice(persons)
            c = self._victim()
            ts = self._time(rng.randint(1, self.cfg.days - 1), rng.choice(c["typical_hours"]))
            iban = self._payee(record["name"], kind="sanctioned", opened=self.cfg.start)
            tx = self._base_tx(c, ts, c["avg_amount"] * rng.uniform(0.8, 4))
            tx.update(label=1, typology="sanctions", exclude_from_training=True)
            self._emit(self._to(tx, iban))

    # --- assembly ---------------------------------------------------------------------------
    def label_noise(self) -> None:
        """Flip a small share of labels (missed chargebacks, friendly fraud)."""
        rng = self.rng
        for tx in self.tx:
            if tx.get("exclude_from_training"):
                continue
            if rng.random() < self.cfg.label_noise_rate:
                tx["label"] = 1 - int(tx["label"])
                tx["label_noise"] = True

    def finish(self) -> SyntheticDataset:
        self.tx.sort(key=lambda t: (t["ts"], t["customer_id"], t["amount"]))
        for i, tx in enumerate(self.tx):
            tx["transaction_id"] = f"SYN-{i + 1:06d}"
            tx.setdefault("exclude_from_training", False)
        customers = [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.customers]
        return SyntheticDataset(
            self.cfg,
            customers,
            self.tx,
            sorted(self.payees.values(), key=lambda p: p["iban"]),
            dict(self.roles),
            self.rings,
        )


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def generate(cfg: SyntheticConfig | None = None) -> SyntheticDataset:
    cfg = cfg or SyntheticConfig()
    builder = _Builder(cfg)
    builder.build_customers()
    builder.assign_mule_rings()
    builder.normal_activity()
    builder.ato()
    builder.app()
    builder.layering_cycles()
    builder.card_testing()
    builder.structuring()
    builder.sanctions()
    builder.label_noise()
    return builder.finish()


#: Keys that exist only for training/evaluation and must be stripped before a
#: synthetic transaction is sent through the API/bus (``TransactionIn`` forbids
#: extra fields).
LABEL_KEYS = ("label", "typology", "exclude_from_training", "label_noise")


def strip_labels(tx: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in tx.items() if k not in LABEL_KEYS}
