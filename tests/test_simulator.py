"""Traffic simulator: normal/anomalous traffic and live scenario injection."""

from __future__ import annotations

import fakeredis
import pytest

from app import simulator
from app.api.schemas import TransactionIn
from app.config import BASE_DIR, get_settings

DEMO_CUSTOMERS = BASE_DIR / "data" / "demo" / "customers.json"


def test_generator_produces_valid_payloads():
    import json

    customers = json.loads(DEMO_CUSTOMERS.read_text(encoding="utf-8"))[:10]
    gen = simulator.TrafficGenerator(customers, seed=1)
    normal = [gen.next(anomaly_rate=0.0) for _ in range(20)]
    weird = [gen.next(anomaly_rate=1.0) for _ in range(40)]
    for tx in normal + weird:
        TransactionIn.model_validate(tx)
    assert all(tx["country"] == "TR" for tx in normal)
    assert any(tx["country"] != "TR" or tx["device_id"].startswith("DEV-NEW") for tx in weird)


async def test_run_publishes_traffic_and_scenarios(monkeypatch):
    """Deterministic: seeded generator/scenarios and an injected clock.

    The old assertion (``sent > 20``) was flaky: the scenario was picked at
    random and one of 18 transactions made ``sent`` exactly 20.
    """
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    get_settings.cache_clear()
    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: fake)
    clock = iter(range(0, 10_000, 10))  # every call advances 10 "seconds"
    monkeypatch.setattr(simulator, "monotonic", lambda: next(clock))
    try:
        sent = await simulator.run(
            rate=0,
            count=20,
            anomaly_rate=0.2,
            customers_path=DEMO_CUSTOMERS,
            scenario_every=15,
            seed=3,
        )
        stream = f"{get_settings().redis_stream_prefix}:transaction.created"
        entries = await fake.xrange(stream)
    finally:
        get_settings.cache_clear()
    keys = [fields.get("key", "") for _, fields in entries]
    assert len(keys) == sent >= 20
    assert any(k.startswith("SIM-") for k in keys)
    assert any(k.startswith("SCN-") for k in keys)  # a scenario was injected


async def test_run_requires_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit):
            await simulator.run(rate=0, count=1, anomaly_rate=0, customers_path=DEMO_CUSTOMERS)
    finally:
        get_settings.cache_clear()
