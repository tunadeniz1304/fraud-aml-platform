"""Streaming feature store tests (P0.4).

Every registered feature has at least one focused test; windows exclude
out-of-window events; memory is bounded; the Redis backend yields exactly the
same features as the in-memory one (online/offline consistency).
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Any

import fakeredis
import pytest

from app.features.definitions import FEATURES, compute_features, describe, feature_names
from app.features.extractor import CustomerDirectory, FeatureExtractor
from app.features.profile import learn
from app.features.store import MemoryFeatureStore, RedisFeatureStore
from app.features.types import CustomerStatic, ProfileState, TxView

T0 = datetime(2026, 9, 22, 14, 0, 0)
CUSTOMERS = [
    {
        "customer_id": "C1",
        "name": "Test Müşteri",
        "avg_amount": 1000,
        "known_device_ids": ["D1"],
        "known_locations": ["İstanbul"],
        "typical_hours": list(range(9, 19)),
        "known_channels": ["mobile"],
        "home_country": "TR",
        "birth_year": 1990,
    },
    {
        "customer_id": "C2",
        "name": "Yaşlı Müşteri",
        "avg_amount": 500,
        "known_device_ids": ["D2"],
        "known_locations": ["Ankara"],
        "typical_hours": list(range(9, 19)),
        "known_channels": ["web"],
        "birth_year": 1950,
    },
]


def tx(i: int, *, minutes: float = 0, **kw: Any) -> dict[str, Any]:
    base = {
        "transaction_id": f"T{i}",
        "ts": (T0 + timedelta(minutes=minutes)).isoformat(),
        "customer_id": "C1",
        "amount": 1000,
        "currency": "TRY",
        "device_id": "D1",
        "location": "İstanbul",
        "country": "TR",
        "channel": "mobile",
        "beneficiary_id": "B1",
        "ip_address": "88.241.1.1",
        "purpose": "Kira",
    }
    base.update(kw)
    return base


def new_extractor(store=None) -> FeatureExtractor:
    return FeatureExtractor(store or MemoryFeatureStore(), CustomerDirectory(CUSTOMERS))


async def run(events: list[dict[str, Any]], target: dict[str, Any], *, learn_profile: bool = True):
    ex = new_extractor()
    for event in events:
        extraction = await ex.extract(event)
        await ex.commit(extraction, learn_profile=learn_profile)
    return (await ex.extract(target)).features


# --- registry -----------------------------------------------------------------------
def test_registry_is_stable_and_described():
    names = feature_names()
    assert names[0] == "amount_try" and len(names) == len(set(names)) >= 40
    for name in FEATURES:
        assert describe(name) and describe(name) != name


# --- amount ------------------------------------------------------------------------------
async def test_amount_features_normalise_currency():
    f = await run([], tx(1, amount=100, currency="EUR"))
    assert f["amount_try"] == 4500.0
    assert f["amount_log"] == pytest.approx(math.log1p(4500))
    assert f["amount_ratio"] == pytest.approx(4500 / 1000, rel=1e-6)
    assert f["amount_zscore"] > 2.0


async def test_first_large_transfer():
    history = [tx(i, minutes=i, amount=900) for i in range(5)]
    assert (await run(history, tx(9, minutes=10, amount=1500)))["first_large_transfer"] == 0.0
    assert (await run(history, tx(9, minutes=10, amount=9000)))["first_large_transfer"] == 1.0


async def test_structuring_features():
    near = [tx(i, minutes=i, amount=47_000, beneficiary_id=f"B{i}") for i in range(3)]
    f = await run(near, tx(9, minutes=10, amount=48_500))
    assert f["near_threshold"] == 1.0 and f["near_threshold_cnt_24h"] == 4.0
    assert (await run(near, tx(9, minutes=10, amount=50_000)))["near_threshold"] == 0.0


# --- velocity + windows -------------------------------------------------------------------------
async def test_velocity_windows_and_sums():
    history = [
        tx(1, minutes=-60 * 24 * 8, amount=111),  # 8 days ago: outside every window
        tx(2, minutes=-60 * 24 * 3, amount=200),  # 3 days: 7d only
        tx(3, minutes=-120, amount=300),  # 2 hours: 24h + 7d
        tx(4, minutes=-30, amount=400),  # 30 min: 1h, 24h, 7d
        tx(5, minutes=-0.5, amount=500),  # 30 s: all
    ]
    f = await run(history, tx(9))
    assert (f["cnt_1m"], f["cnt_1h"], f["cnt_24h"], f["cnt_7d"]) == (1, 2, 3, 4)
    assert (f["sum_1m"], f["sum_1h"], f["sum_24h"], f["sum_7d"]) == (500, 900, 1200, 1400)
    assert f["time_since_last_s"] == pytest.approx(30)


async def test_future_events_are_not_counted():
    f = await run([tx(1, minutes=10)], tx(9))
    assert f["cnt_7d"] == 0 and f["time_since_last_s"] == 7 * 86400


# --- payee ----------------------------------------------------------------------------------------
async def test_payee_features():
    history = [
        tx(1, minutes=-60 * 24 * 2, beneficiary_id="B1"),
        tx(2, minutes=-10, beneficiary_id="B2"),
        tx(3, minutes=-5, beneficiary_id="B3"),
    ]
    known = await run(history, tx(9, beneficiary_id="B1"))
    assert known["is_new_payee"] == 0.0
    assert known["payee_relation_age_d"] == pytest.approx(2.0)
    assert known["distinct_payees_24h"] == 2.0 and known["distinct_payees_7d"] == 3.0
    fresh = await run(history, tx(9, beneficiary_id="B-NEW"))
    assert fresh["is_new_payee"] == 1.0 and fresh["payee_relation_age_d"] == -1.0
    assert fresh["payee_age_d"] == -1.0


async def test_payee_fan_in_counts_distinct_senders_in_24h():
    history = [
        tx(1, minutes=-60 * 25, customer_id="C2", device_id="D2", beneficiary_id="MULE"),
        tx(2, minutes=-60, customer_id="C2", device_id="D2", beneficiary_id="MULE"),
        tx(3, minutes=-30, customer_id="X9", device_id="D9", beneficiary_id="MULE"),
    ]
    f = await run(history, tx(9, beneficiary_id="MULE"))
    assert f["payee_fan_in_24h"] == 3.0  # C2, X9 and the current sender C1
    assert f["payee_age_d"] == pytest.approx(25 / 24)


# --- device / network -----------------------------------------------------------------------------
async def test_device_features():
    history = [
        tx(1, minutes=-60 * 24, customer_id="C2", device_id="SHARED"),
        tx(2, minutes=-60, customer_id="X9", device_id="SHARED"),
    ]
    f = await run(history, tx(9, device_id="SHARED"))
    assert f["is_new_device"] == 1.0 and f["device_customers_7d"] == 3.0
    assert f["device_age_d"] == pytest.approx(1.0)
    own = await run([], tx(9))
    assert own["is_new_device"] == 0.0 and own["device_customers_7d"] == 1.0


async def test_new_device_becomes_known_after_trusted_use():
    f = await run([tx(1, minutes=-5, device_id="D-NEW")], tx(9, device_id="D-NEW"))
    assert f["is_new_device"] == 0.0
    untrusted = await run(
        [tx(1, minutes=-5, device_id="D-NEW")], tx(9, device_id="D-NEW"), learn_profile=False
    )
    assert untrusted["is_new_device"] == 1.0  # poisoning protection


async def test_location_and_ip_features():
    f = await run([], tx(9, location="Lagos", country="NG", ip_address="88.241.1.1"))
    assert f["is_new_location"] == 1.0 and f["is_foreign"] == 1.0
    assert f["is_high_risk_country"] == 1.0 and f["ip_country_mismatch"] == 1.0
    vpn = await run([], tx(9, ip_address="45.84.10.10"))
    assert vpn["is_anonymized_ip"] == 1.0
    clean = await run([], tx(9))
    assert clean["ip_country_mismatch"] == 0.0 and clean["is_anonymized_ip"] == 0.0


# --- time / channel ------------------------------------------------------------------------------
async def test_time_features():
    history = [tx(1)]  # moved to 02:00 the same day below
    history[0]["ts"] = datetime(2026, 9, 22, 2, 0).isoformat()
    f = await run(history, tx(9, ts=datetime(2026, 9, 22, 3, 0).isoformat()))
    assert f["hour"] == 3.0 and f["is_night"] == 1.0
    assert f["night_ratio_7d"] == 1.0 and f["hour_unusual"] == 1.0
    day = await run([], tx(9))
    assert day["is_night"] == 0.0 and day["hour_unusual"] == 0.0


async def test_channel_features():
    f = await run([tx(1, minutes=-5, channel="mobile")], tx(9, channel="web"))
    assert f["channel_change"] == 1.0 and f["channel_unknown"] == 1.0
    same = await run([tx(1, minutes=-5)], tx(9))
    assert same["channel_change"] == 0.0 and same["channel_unknown"] == 0.0


# --- session / behavioural biometrics -------------------------------------------------------------
async def test_session_features():
    session = {
        "login_to_transfer_s": 12,
        "session_duration_s": 900,
        "paste_used": True,
        "is_emulator": True,
        "is_rooted": True,
        "remote_access_tool": True,
        "active_call": True,
    }
    f = await run([], tx(9, session=session))
    assert f["login_to_transfer_s"] == 12 and f["session_duration_s"] == 900
    for name in ("paste_used", "is_emulator", "is_rooted", "remote_access", "active_call"):
        assert f[name] == 1.0, name
    empty = await run([], tx(9))
    assert empty["login_to_transfer_s"] == -1 and empty["remote_access"] == 0.0


async def test_typing_deviation_against_learned_rhythm():
    history = [
        tx(i, minutes=-60 + i, session={"typing_cadence_ms": 180 + (i % 3) * 5}) for i in range(8)
    ]
    normal = await run(history, tx(9, session={"typing_cadence_ms": 185}))
    bot = await run(history, tx(9, session={"typing_cadence_ms": 40}))
    assert normal["typing_deviation"] < 2.0 < bot["typing_deviation"]
    assert (await run([], tx(9, session={"typing_cadence_ms": 40})))["typing_deviation"] == 0.0


# --- text / customer ------------------------------------------------------------------------------
async def test_text_and_customer_features():
    f = await run([], tx(9, purpose="ACİL: güvenli hesaba aktar"))
    assert f["purpose_risk"] == 1.0
    assert (await run([], tx(9)))["purpose_risk"] == 0.0
    old = await run([], tx(9, customer_id="C2", device_id="D2"))
    assert old["vulnerable_customer"] == 1.0
    assert (await run([], tx(9)))["vulnerable_customer"] == 0.0


def test_every_feature_has_a_test_above():
    """Guard: a new feature must come with a test (names referenced in this module)."""
    source = open(__file__, encoding="utf-8").read()
    missing = [n for n in FEATURES if f'"{n}"' not in source]
    assert not missing, f"testsiz feature: {missing}"


# --- profile --------------------------------------------------------------------------------------
def test_profile_ewma_moves_toward_new_behaviour():
    customer = CustomerStatic.from_record(CUSTOMERS[0])
    profile = ProfileState.from_static(customer, prior_weight=5)
    start = profile.log_mean
    view = TxView.from_payload(tx(1, amount=5000))
    for _ in range(20):
        profile = learn(profile, view, alpha_min=0.05)
    assert profile.log_mean > start and profile.n == 25
    assert profile.max_amount == 5000
    assert profile.hour_hist[14] > profile.hour_hist[3]


def test_profile_device_map_is_bounded():
    customer = CustomerStatic.from_record(CUSTOMERS[0])
    profile = ProfileState.from_static(customer, prior_weight=5)
    for i in range(50):
        profile = learn(profile, TxView.from_payload(tx(i, device_id=f"D{i}")), alpha_min=0.05)
    assert len(profile.devices) <= 20


# --- memory bounds + redis parity -----------------------------------------------------------------
async def test_memory_store_is_bounded():
    store = MemoryFeatureStore(max_entities=10)
    ex = new_extractor(store)
    for i in range(200):
        event = tx(i, minutes=i, customer_id=f"X{i}", device_id=f"D{i}", beneficiary_id=f"B{i}")
        await ex.commit(await ex.extract(event), learn_profile=True)
    sizes = store.size()
    assert max(sizes.values()) <= 40  # pairs cap is 4x


def _random_stream(n: int, seed: int = 7) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = []
    minute = 0.0
    for i in range(n):
        minute += rng.expovariate(1 / 20)
        out.append(
            tx(
                i,
                minutes=minute,
                customer_id=rng.choice(["C1", "C2", "X1"]),
                device_id=rng.choice(["D1", "D2", "D3"]),
                beneficiary_id=rng.choice(["B1", "B2", "B3", "B4", ""]),
                amount=round(rng.lognormvariate(7, 1), 2),
                channel=rng.choice(["mobile", "web"]),
                session={"typing_cadence_ms": rng.randint(120, 260)},
            )
        )
    return out


async def test_redis_backend_matches_memory_backend():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    memory_ex = new_extractor(MemoryFeatureStore())
    redis_ex = new_extractor(RedisFeatureStore(redis))
    for i, event in enumerate(_random_stream(150)):
        a = await memory_ex.extract(event)
        b = await redis_ex.extract(event)
        assert a.features == pytest.approx(b.features), f"event {i}"
        trusted = i % 4 != 0
        await memory_ex.commit(a, learn_profile=trusted)
        await redis_ex.commit(b, learn_profile=trusted)


async def test_redis_keys_carry_ttl():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ex = new_extractor(RedisFeatureStore(redis))
    await ex.commit(await ex.extract(tx(1)), learn_profile=True)
    for key in await redis.keys("fs:*"):
        if key not in ("fs:payee_first", "fs:dev_first"):
            assert await redis.ttl(key) > 0, key


def test_compute_features_reads_external_extras():
    from app.features.definitions import EXTERNAL_FEATURES, register_external

    register_external("graph_test_signal", "test sinyali")
    try:
        customer = CustomerStatic.from_record(CUSTOMERS[0])
        from app.features.types import FeatureContext, StateSnapshot

        ctx = FeatureContext(
            tx=TxView.from_payload(tx(1)),
            customer=customer,
            state=StateSnapshot(),
            profile=ProfileState.from_static(customer, 5),
            ip=None,
            extras={"graph_test_signal": 0.7},
        )
        assert compute_features(ctx)["graph_test_signal"] == 0.7
    finally:
        EXTERNAL_FEATURES.pop("graph_test_signal", None)
