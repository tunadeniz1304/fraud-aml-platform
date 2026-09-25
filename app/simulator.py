"""Transaction traffic simulator (compose ``simulator`` service).

Publishes a continuous, realistic stream of transfers for the configured
customers onto the Redis ingress stream (``fraud:transaction.created``) — a
stand-in for a core-banking / payment-switch feed. Mostly normal behaviour
(known device, usual hours, usual payees), with a configurable share of
anomalies (new device, foreign IP, large amount, urgent purpose).

    python -m app.simulator --rate 2
    python -m app.simulator --rate 50 --count 5000 --anomaly-rate 0.05
    python -m app.simulator --rate 2 --scenario-every 180   # + saldırı senaryoları

With ``--scenario-every N`` a random attack scenario (ATO, APP, mule ring,
smurfing, card testing — :mod:`app.scenarios`) is injected every N seconds so
the live dashboard always has something to investigate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import uuid
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from app.agents.transaction_monitor import TransactionMonitor
from app.config import get_settings
from app.features.extractor import CustomerDirectory
from app.monitoring.logging import configure_logging
from app.scenarios import SCENARIOS, ScenarioError, ScenarioFactory

logger = logging.getLogger("fraud.simulator")

_FOREIGN = [
    ("Lagos", "NG", "197.210.10.10"),
    ("Berlin", "DE", "89.12.1.1"),
    ("Dubai", "AE", "92.96.1.1"),
]
_VPN_IPS = ["45.84.3.7", "104.244.73.1", "185.220.101.5"]
_URGENT = ["ACİL güvenli hesaba aktarım", "Yatırım fırsatı - hemen", "Vergi borcu ödemesi acil"]
_NORMAL_PURPOSES = ["Kira", "Market", "Fatura", "Aile desteği", "Aidat", "Eğitim", "Alışveriş"]
_DOMESTIC_IPS = ["88.241.{}.{}", "5.176.{}.{}", "176.40.{}.{}", "78.161.{}.{}"]


class TrafficGenerator:
    def __init__(self, customers: list[dict[str, Any]], *, seed: int | None = None) -> None:
        self.customers = customers
        self.rng = random.Random(seed)
        self.payees = {
            c["customer_id"]: [f"TR{self.rng.randint(10**23, 10**24 - 1)}" for _ in range(4)]
            for c in customers
        }

    def next(self, *, anomaly_rate: float) -> dict[str, Any]:
        rng = self.rng
        customer = rng.choice(self.customers)
        now = datetime.now(UTC).replace(tzinfo=None)
        avg = float(customer.get("avg_amount", 1000))
        tx: dict[str, Any] = {
            "transaction_id": f"SIM-{uuid.uuid4().hex[:12].upper()}",
            "ts": now.isoformat(timespec="seconds"),
            "customer_id": customer["customer_id"],
            "amount": round(max(20.0, rng.lognormvariate(0, 0.45) * avg), 2),
            "currency": "TRY",
            "device_id": rng.choice(customer.get("known_device_ids") or ["DEV-UNKNOWN"]),
            "location": rng.choice(customer.get("known_locations") or ["İstanbul"]),
            "country": "TR",
            "channel": rng.choice(customer.get("known_channels") or ["mobile"]),
            "ip_address": rng.choice(_DOMESTIC_IPS).format(
                rng.randint(1, 250), rng.randint(1, 250)
            ),
            "beneficiary_iban": rng.choice(self.payees[customer["customer_id"]]),
            "purpose": rng.choice(_NORMAL_PURPOSES),
        }
        if rng.random() < anomaly_rate:
            kind = rng.choice(["foreign", "new_device", "large", "urgent"])
            if kind == "foreign":
                tx["location"], tx["country"], tx["ip_address"] = rng.choice(_FOREIGN)
            elif kind == "new_device":
                tx["device_id"] = f"DEV-NEW-{rng.randint(1000, 9999)}"
                tx["ip_address"] = rng.choice(_VPN_IPS)
            elif kind == "large":
                tx["amount"] = round(avg * rng.uniform(8, 30), 2)
                tx["beneficiary_iban"] = f"TR{rng.randint(10**23, 10**24 - 1)}"
            else:
                tx["purpose"] = rng.choice(_URGENT)
                tx["beneficiary_iban"] = f"TR{rng.randint(10**23, 10**24 - 1)}"
        return tx


async def run(
    rate: float,
    count: int | None,
    anomaly_rate: float,
    customers_path: Path,
    scenario_every: float = 0.0,
    *,
    seed: int | None = None,
) -> int:
    import redis.asyncio as aioredis

    from app.bus.redis_streams import RedisStreamsBus

    settings = get_settings()
    if not settings.redis_url:
        raise SystemExit("REDIS_URL tanımlı değil — simülatör Redis Streams'e yayın yapar")
    with customers_path.open("r", encoding="utf-8") as fh:
        customers = json.load(fh)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    bus = RedisStreamsBus(redis, prefix=settings.redis_stream_prefix)
    generator = TrafficGenerator(customers, seed=seed)
    directory = CustomerDirectory(customers)
    delay = 1.0 / rate if rate > 0 else 0.0
    sent = 0
    next_scenario = monotonic() + scenario_every if scenario_every > 0 else None
    logger.info("Simülatör başladı: %.1f işlem/sn, anomali oranı %.0f%%", rate, anomaly_rate * 100)
    try:
        while count is None or sent < count:
            tx = generator.next(anomaly_rate=anomaly_rate)
            await bus.publish(TransactionMonitor.CREATED, tx, key=tx["transaction_id"])
            sent += 1
            if next_scenario is not None and monotonic() >= next_scenario:
                name = generator.rng.choice(list(SCENARIOS))
                try:
                    scenario = ScenarioFactory(directory, seed=seed).build(name)
                except ScenarioError as exc:
                    logger.warning("Senaryo atlandı (%s): %s", name, exc)
                else:
                    for stx in scenario.transactions:
                        await bus.publish(
                            TransactionMonitor.CREATED, stx, key=stx["transaction_id"]
                        )
                    sent += len(scenario.transactions)
                    logger.info(
                        "Senaryo enjekte edildi: %s (%s)", scenario.title, scenario.customers
                    )
                next_scenario = monotonic() + scenario_every
            if sent % 100 == 0:
                logger.info("Simülatör: %d işlem yayınlandı", sent)
            if delay:
                await asyncio.sleep(delay * generator.rng.uniform(0.5, 1.5))
    finally:
        await redis.aclose()
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--rate", type=float, default=2.0, help="saniyedeki işlem")
    parser.add_argument("--count", type=int, default=None, help="toplam işlem (boş: sonsuz)")
    parser.add_argument("--anomaly-rate", type=float, default=0.05)
    parser.add_argument("--customers", type=Path, default=None)
    parser.add_argument(
        "--scenario-every", type=float, default=0.0, help="saniye (0: senaryo enjeksiyonu yok)"
    )
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings)
    asyncio.run(
        run(
            args.rate,
            args.count,
            args.anomaly_rate,
            args.customers or settings.resolved_customers_path,
            args.scenario_every,
        )
    )


if __name__ == "__main__":
    main()
