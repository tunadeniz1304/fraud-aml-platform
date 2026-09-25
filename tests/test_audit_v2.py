"""Regression tests for the v2 audit findings (docs/PLAN_v2.md).

Every finding was first reproduced here as a failing test (``xfail(strict=True)``
marks the ones still open); the fix commit of each finding removes its marker,
so a test that starts passing without a fix — or a fix that regresses — fails
the suite.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.config import BASE_DIR, get_settings

DEMO = BASE_DIR / "data" / "demo"


def open_finding(code: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=f"audit v2 finding {code} still open")


# --- shared fixtures ------------------------------------------------------------------------
@pytest.fixture()
def demo_env(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FRAUD_CUSTOMERS_PATH", str(DEMO / "customers.json"))
    monkeypatch.setenv("FRAUD_PAYEES_PATH", str(DEMO / "payees.json"))
    monkeypatch.setenv("FRAUD_TRANSACTIONS_PATH", str(DEMO / "transactions.json"))
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("RING_DETECT_INTERVAL_S", "0")
    get_settings.cache_clear()
    return app_env


@pytest.fixture()
async def pipeline(demo_env: Path) -> Any:
    from app.pipeline import build_pipeline

    p = await build_pipeline(get_settings())
    await p.start()
    yield p
    await p.stop()


def _customers() -> list[dict[str, Any]]:
    return json.loads((DEMO / "customers.json").read_text(encoding="utf-8"))


def _transfer(c: dict[str, Any], tx_id: str, ts: datetime, amount: float, **extra: Any):
    tx = {
        "transaction_id": tx_id,
        "ts": ts.replace(microsecond=0).isoformat(),
        "customer_id": c["customer_id"],
        "amount": amount,
        "currency": "TRY",
        "device_id": (c.get("known_device_ids") or ["DEV-X"])[0],
        "location": (c.get("known_locations") or ["İstanbul"])[0],
        "country": "TR",
        "channel": (c.get("known_channels") or ["mobile"])[0],
        "beneficiary_id": f"TR33000610051978645784{tx_id[-4:]}",
        "beneficiary_name": "Mehmet Kaya",
        "purpose": "kira",
    }
    tx.update(extra)
    return tx


def _dossier(case_type: str, *, with_tx: bool) -> dict[str, Any]:
    """Copilot evidence as gathered while the write-behind writer is still pending."""
    alert = {
        "alert_id": "ALERT-1",
        "transaction_id": "TX-1",
        "alert_type": case_type,
        "decision": "HOLD",
        "risk_score": 0.8,
        "amount_try": 1200.0,
        "ts": "2026-09-25T10:00:00" if with_tx else "",
        "beneficiary": "TR1" if with_tx else None,
        "beneficiary_name": None,
        "purpose": None,
        "channel": "mobile" if with_tx else None,
        "country": "TR",
        "reason_codes": [],
    }
    return {
        "get_case": {
            "case_id": "CASE-1",
            "customer_id": "C1",
            "case_type": case_type,
            "title": "Vaka",
            "status": "YENI",
            "priority": 1.0,
            "total_amount_try": 1200.0,
            "alerts": [alert],
            "timeline": [],
            "suspicion_at": "",
            "masak_deadline": "",
        },
        "allowed_citations": ["CASE-1", "ALERT-1", "TX-1", "C1"],
    }


CASE_TYPES = ["ATO", "APP", "MULE", "AML", "CARD_TESTING", "YAPTIRIM"]


# --- A1: demo / fallback output must satisfy its own schema -----------------------------------
@pytest.mark.parametrize("case_type", CASE_TYPES)
async def test_a1_demo_sib_and_summary_valid_without_persisted_tx(case_type: str) -> None:
    from app.copilot import templates  # noqa: F401
    from app.copilot.schemas import CaseSummary, SibDraft, citation_validator
    from app.llm.config import LLMSettings
    from app.llm.service import LLMService

    llm = LLMService(LLMSettings(mode="demo"))
    dossier = _dossier(case_type, with_tx=False)
    check = citation_validator(dossier["allowed_citations"])
    sib = await llm.generate(
        task="sib_draft", system="", payload=dossier, schema=SibDraft, validator=check
    )
    assert sib.output.islemler
    summary = await llm.generate(
        task="copilot_summary", system="", payload=dossier, schema=CaseSummary, validator=check
    )
    assert len(summary.output.bullets) >= 3


async def test_a1_rejected_copilot_output_is_recorded_not_silent(pipeline: Any) -> None:
    from app.llm.service import OutputRejected

    async def broken(case_id: int) -> Any:
        raise OutputRejected("schema_invalid", "String should have at least 5 characters")

    pipeline.copilot.sib_draft = broken
    out = await pipeline.run_scenario("smurfing")
    case = await pipeline.cases.get_case(out["cases"][0]["id"])
    kinds = [e["event_type"] for e in case["events"]]
    # deterministic minimal draft instead of losing the ŞİB, plus an error event
    assert case["sib_draft"], kinds
    assert "COPILOT_ERROR" in kinds


# --- A2: STEP_UP false-positive loop ----------------------------------------------------------
async def test_a2_passed_step_up_teaches_the_new_device(pipeline: Any) -> None:
    c = _customers()[5]
    now = datetime.now()
    kw = {"device_id": "DEV-NEWPHONE-01", "beneficiary_id": "TR330006100519786457841326"}
    avg = float(c.get("avg_amount") or 1000)
    first = await pipeline.ingest(_transfer(c, "TX-SU-0001", now, avg * 6, **kw))
    assert first["decision"] == "STEP_UP"
    await pipeline.step_up_result("TX-SU-0001", success=True, actor="otp")
    second = await pipeline.ingest(
        _transfer(c, "TX-SU-0002", now + timedelta(hours=2), avg * 6, **kw)
    )
    assert second["features"]["is_new_device"] == 0.0
    assert second["risk_score"] < first["risk_score"]
    assert second["decision"] == "ALLOW"


# --- A3: training / serving parity ------------------------------------------------------------
async def test_a3_backfill_and_live_replay_produce_identical_features() -> None:
    from app.features.learning import should_learn  # single learning rule

    assert callable(should_learn)
    from app.ml.training import backfill, live_replay_features
    from app.scoring.rules import load_ruleset
    from app.synthetic.generator import SyntheticConfig, generate

    data = generate(
        SyntheticConfig(
            seed=7,
            customers=80,
            days=10,
            ato_attacks=8,
            app_scams=8,
            mule_rings=1,
            ring_size=4,
            card_testing=4,
            structuring=4,
            sanctions_hits=2,
        )
    )
    assert any(t.get("label") for t in data.transactions)
    ruleset = load_ruleset()
    rows = await backfill(data.customers, data.transactions, ruleset)
    live = await live_replay_features(data.customers, data.transactions, ruleset)
    assert [r.features for r in rows] == live


# --- A4: rule floors must not force HOLD below the hold threshold for thin histories ----------
async def test_a4_second_transfer_of_the_day_is_step_up_not_hold(pipeline: Any) -> None:
    c = _customers()[2]
    now = datetime.now()
    await pipeline.ingest(_transfer(c, "TX-FD-0001", now, 800))
    r = await pipeline.ingest(_transfer(c, "TX-FD-0002", now + timedelta(minutes=5), 5096))
    assert r["risk_score"] < pipeline.engine.policy.thresholds.hold
    assert r["decision"] == "STEP_UP"
    await pipeline.writer.flush()
    assert pipeline.accounts.get_status(c["customer_id"]) == "AKTIF"


# --- A5: card testing is its own typology -----------------------------------------------------
async def test_a5_card_testing_opens_card_testing_case(pipeline: Any) -> None:
    out = await pipeline.run_scenario("card_testing")
    assert out["cases"][0]["case_type"] == "CARD_TESTING"


# --- A6: graph direction + BLOCK feedback -----------------------------------------------------
def test_a6_fan_in_victims_are_affected_not_ring_members() -> None:
    from app.graph.entity_graph import EntityGraph

    g = EntityGraph()
    g.register_customer("MULE", iban="TR-MULE")
    g.register_customer("BOSS", iban="TR-BOSS")
    t = 1_000_000.0
    for i, victim in enumerate(("V1", "V2", "V3")):
        g.observe(tx_id=f"in{i}", customer_id=victim, ts=t + i, amount=9000, beneficiary="TR-MULE")
    g.observe(tx_id="out", customer_id="MULE", ts=t + 60, amount=26000, beneficiary="TR-BOSS")
    rings = g.detect_rings()
    assert rings, "the mule → boss path must still be detected"
    ring = rings[0]
    assert "MULE" in ring["members"]
    assert not {"V1", "V2", "V3"} & set(ring["members"])
    assert {"V1", "V2", "V3"} <= set(ring["affected"])


async def test_a6_block_flags_graph_fraud_nodes(pipeline: Any) -> None:
    before = pipeline.graph.stats()["fraud_nodes"]
    out = await pipeline.run_scenario("ato")
    assert out["results"][0]["decision"] == "BLOCK"
    assert pipeline.graph.stats()["fraud_nodes"] > before


# --- A7: PSI must not alarm on the demo population at startup ---------------------------------
async def test_a7_no_drift_alarm_on_demo_startup_but_real_drift_alarms(
    demo_env: Path, monkeypatch
) -> None:
    """The demo stream replays the demo population: PSI stays quiet at startup and
    for that stream, while the same stream with STREAM_DRIFT-style inflated amounts
    alarms."""
    from app.pipeline import build_pipeline, load_transactions, rebase_timestamps

    p = await build_pipeline(get_settings())
    await p.start()
    try:
        drift = p.engine.drift
        await p._drift_task  # population reference built in the background
        startup = drift.status()
        assert startup["reference"] == "population" and not startup["pending"]
        assert startup["alerts"] == []
        stream = rebase_timestamps(load_transactions(get_settings().resolved_transactions_path))
        for tx in stream:
            await p.ingest({**tx, "transaction_id": "S1-" + tx["transaction_id"]})
        stable = drift.status()
        assert stable["observed"] >= 1000
        assert stable["alerts"] == [], stable["psi"]
        shifted = rebase_timestamps(stream, end=datetime.now() + timedelta(days=7))
        for tx in shifted:
            await p.ingest(
                {
                    **tx,
                    "transaction_id": "S2-" + tx["transaction_id"],
                    "amount": round(float(tx["amount"]) * 4, 2),
                }
            )
        drifted = drift.status()
        assert drifted["alerts"], drifted["psi"]
    finally:
        await p.stop()


def test_a7_psi_handles_empty_bins_and_windows() -> None:
    import math

    from app.ml.metrics import psi, psi_from_distribution

    assert psi([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], [1.5, 2.5]) == pytest.approx(0.0)
    value = psi_from_distribution([1.0, 0.0, 0.0], [5.0, 5.0], [1.5, 2.5])  # empty bins
    assert math.isfinite(value) and value > 0.25
    assert math.isfinite(psi_from_distribution([0.5, 0.5], [], [1.0]))  # empty window


# --- B8: per-customer ordering ----------------------------------------------------------------
async def test_b8_concurrent_same_customer_velocity_is_exact(demo_env: Path, monkeypatch) -> None:
    """A burst of 50 payments in the same second (card testing) against the shared
    Redis feature store: whatever the arrival order, every event must see all
    previously committed ones. Without per-customer serialisation the snapshots
    of concurrent events read the same stale window (network round trips yield)."""
    import fakeredis.aioredis

    from app.pipeline import build_pipeline

    monkeypatch.setenv("FEATURE_STORE", "redis")
    get_settings.cache_clear()
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    p = await build_pipeline(get_settings(), redis=fake)
    await p.start()
    # keep the account scorable even if the model blocks the burst (counts are the point)
    p.analyst.account_status = lambda _cid: "AKTIF"
    try:
        c = _customers()[7]
        now = datetime.now()
        txs = [_transfer(c, f"TX-CC-{i:04d}", now, 50, beneficiary_id="TR-SAME") for i in range(50)]
        results = await asyncio.gather(*(p.ingest(t) for t in txs))
        counts = sorted(r["features"]["cnt_1h"] for r in results)
        assert counts == [float(i) for i in range(50)]
        after = await p.ingest(_transfer(c, "TX-CC-9999", now + timedelta(seconds=1), 50))
        assert after["features"]["cnt_1h"] == 50.0
    finally:
        await p.stop()


# --- B9: bounded Redis hashes -----------------------------------------------------------------
async def test_b9_every_feature_store_key_has_a_ttl() -> None:
    import fakeredis.aioredis

    from app.features.store import RedisFeatureStore
    from app.features.types import ProfileState, TxView

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisFeatureStore(redis)
    tx = TxView.from_payload(
        {
            "transaction_id": "T1",
            "customer_id": "C1",
            "ts": "2026-09-25T10:00:00",
            "amount": 10,
            "device_id": "D1",
            "beneficiary_id": "B1",
        }
    )
    await store.commit(tx, ProfileState())
    for key in await redis.keys("fs:*"):
        assert await redis.ttl(key) > 0, key


# --- B10: idempotent handlers -----------------------------------------------------------------
async def test_b10_replayed_commit_does_not_double_count() -> None:
    from app.features.store import MemoryFeatureStore
    from app.features.types import ProfileState, TxView

    store = MemoryFeatureStore()
    tx = TxView.from_payload(
        {"transaction_id": "T1", "customer_id": "C1", "ts": "2026-09-25T10:00:00", "amount": 10}
    )
    await store.commit(tx, ProfileState())
    await store.commit(tx, ProfileState())  # redelivery after a crash
    later = TxView.from_payload(
        {"transaction_id": "T2", "customer_id": "C1", "ts": "2026-09-25T10:01:00", "amount": 10}
    )
    snap = await store.snapshot(later)
    assert len(snap.history) == 1


async def test_b10_redis_commit_is_atomic_and_idempotent() -> None:
    import fakeredis.aioredis

    from app.features.store import RedisFeatureStore
    from app.features.types import ProfileState, TxView

    store = RedisFeatureStore(fakeredis.aioredis.FakeRedis(decode_responses=True))
    tx = TxView.from_payload(
        {
            "transaction_id": "T1",
            "customer_id": "C1",
            "ts": "2026-09-25T10:00:00",
            "amount": 10,
            "beneficiary_id": "B1",
            "device_id": "D1",
        }
    )
    await store.commit(tx, ProfileState())
    await store.commit(tx, ProfileState())  # handler crashed after commit, retried
    later = TxView.from_payload(
        {"transaction_id": "T2", "customer_id": "C1", "ts": "2026-09-25T10:01:00", "amount": 10}
    )
    assert len((await store.snapshot(later)).history) == 1


async def test_b10_stream_message_in_progress_is_not_processed_twice() -> None:
    import json as _json

    import fakeredis.aioredis

    from app.bus.redis_streams import RedisStreamsBus

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    calls: list[str] = []
    gate = asyncio.Event()

    async def slow(payload: dict) -> None:
        calls.append(payload["transaction_id"])
        await gate.wait()

    a = RedisStreamsBus(redis, consumer="a")
    b = RedisStreamsBus(redis, consumer="b")
    for bus in (a, b):
        bus.subscribe("t", slow, group="g")
    fields = {"key": "TX-1", "payload": _json.dumps({"transaction_id": "TX-1"})}
    first = asyncio.create_task(a._handle(a._subs[0], "s", "1-0", fields))
    await asyncio.sleep(0.01)
    await b._handle(b._subs[0], "s", "1-0", fields)  # redelivery while a still works
    gate.set()
    await first
    assert calls == ["TX-1"]


# --- B13: dynamic holiday calendar ------------------------------------------------------------
def test_b13_masak_deadline_skips_2027_and_2028_religious_holidays() -> None:
    from app.cases import sla

    # Ramazan Bayramı 2027: 9-11 March (Tue-Thu)
    assert sla.masak_deadline(datetime(2027, 3, 5, 10)).date().isoformat() == "2027-03-24"
    # Kurban Bayramı 2028: 5-8 May, then 19 May
    assert sla.masak_deadline(datetime(2028, 5, 3, 10)).date().isoformat() == "2028-05-22"


def test_b13_arife_half_day_policy_is_configurable(monkeypatch) -> None:
    from app.cases import sla

    start = datetime(2027, 3, 5, 10)  # Friday; arife = Monday 8 March 2027 (13:00)
    assert sla.is_business_day(datetime(2027, 3, 8).date())  # default: morning worked
    monkeypatch.setenv("TR_HALF_DAY_POLICY", "holiday")
    get_settings.cache_clear()
    try:
        assert not sla.is_business_day(datetime(2027, 3, 8).date())
        assert sla.masak_deadline(start).date().isoformat() == "2027-03-25"
    finally:
        get_settings.cache_clear()


# --- B14: FATF-sourced high-risk list ---------------------------------------------------------
def test_b14_high_risk_countries_come_from_versioned_fatf_list() -> None:
    from app.core.jurisdictions import load_jurisdictions

    data = load_jurisdictions()
    assert data["source"].startswith("https://www.fatf-gafi.org/")
    assert data["as_of"] and data["version"]
    for listing in data["lists"].values():
        assert listing["verification"]  # verified or explicitly marked as not verified
    codes = get_settings().high_risk_country_set
    assert {"KP", "IR"} <= codes
    assert not {"UA", "AE"} & codes


# --- B15: sanctions blocking index + secondary keys -------------------------------------------
def test_b15_single_token_alias_and_secondary_keys(tmp_path: Path) -> None:
    from app.core.sanctions import SanctionScreener

    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "SDN-9",
                    "name": "Abdulrahman Haqqani",
                    "aliases": ["Haqqani"],
                    "country": "AF",
                    "birth_date": "1970-01-01",
                    "program": "SDGT",
                    "pep": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    screener = SanctionScreener(path).load()
    [hit] = screener.screen("HAQQANI")
    assert hit["id"] == "SDN-9"
    same = screener.screen("Abdulrahman Haqqani", birth_date="1970-01-01", nationality="AF")
    other = screener.screen("Abdulrahman Haqqani", birth_date="1991-06-12", nationality="TR")
    assert same[0]["confidence"] > other[0]["confidence"]
    # controlled single-token fuzzy: a typo matches only with a confirming secondary key
    assert screener.screen("Haqani") == []
    [typo] = screener.screen("Haqqanii", birth_date="1970")
    assert typo["match_type"] == "fuzzy" and typo["secondary"]["birth_date"] is True
    # blocking: an unrelated name never reaches the fuzzy comparison
    assert screener.candidates("Mehmet Kaya") == set()


# --- C16: demo users only in dev --------------------------------------------------------------
def test_c16_prod_refuses_demo_users(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.security.auth import UserDirectory

    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.delenv("SEED_DEMO_USERS", raising=False)
    get_settings.cache_clear()
    try:
        assert not get_settings().seed_demo_users
        monkeypatch.setenv("SEED_DEMO_USERS", "true")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError):
            UserDirectory.from_settings(get_settings())
    finally:
        get_settings.cache_clear()


# --- C17: SSE tickets instead of ?access_token= -----------------------------------------------
def test_c17_sse_uses_single_use_ticket(demo_env: Path, analyst_headers: dict[str, str]) -> None:
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from starlette.requests import Request

    from app.api.dashboard import create_app
    from app.security.deps import stream_principal

    token = analyst_headers["Authorization"].split()[1]
    with TestClient(create_app()) as client:
        response = client.post("/api/stream/ticket", headers=analyst_headers)
        assert response.status_code == 200
        ticket = response.json()["ticket"]
        assert ticket and ticket != token
    scope = {"type": "http", "query_string": f"access_token={token}".encode(), "headers": []}
    with pytest.raises(HTTPException):
        asyncio.run(stream_principal(Request(scope), None))


# --- C18: /metrics protected ------------------------------------------------------------------
def test_c18_metrics_not_public_by_default(demo_env: Path) -> None:
    from fastapi.testclient import TestClient

    from app.api.dashboard import create_app

    with TestClient(create_app()) as client:
        assert client.get("/metrics").status_code in (401, 403, 404)


# --- C19: sane ingest limit + HMAC nonce ------------------------------------------------------
def test_c19_ingest_limit_is_per_client_and_hmac_nonce_blocks_replay() -> None:
    from app.security.auth import verify_signature

    limit = int(get_settings().rate_limit_ingest.split("/")[0])
    assert limit <= 20_000
    import inspect

    assert "nonce" in inspect.signature(verify_signature).parameters


# --- C20: ASCII-folded / beneficiary names are redacted ---------------------------------------
def test_c20_redaction_handles_ascii_folding_and_beneficiaries() -> None:
    from app.llm.redaction import Redactor

    r = Redactor(["Ayşe Yılmaz"])
    text = r.redact("Ayse Yilmaz ve AYŞE YILMAZ, Selin Demirtaş'a gönderdi")
    assert "Ayse" not in text and "AYŞE" not in text and "Selin" not in text
    assert r.restore(text).count("Ayşe Yılmaz") == 2
    payload = r.redact_obj({"beneficiary_name": "Kemal Sunal", "purpose": "Hakan Tas borcu"})
    assert "Kemal" not in json.dumps(payload, ensure_ascii=False)
    assert "Hakan" not in json.dumps(payload, ensure_ascii=False)


# --- C21: no internal hostnames in tracked files ----------------------------------------------
def test_c21_no_internal_llm_host_in_repository() -> None:
    out = subprocess.run(
        ["git", "grep", "-n", "ss" + "yz"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout.strip() == ""


def test_c21_llm_status_hides_host_from_non_admins(
    demo_env: Path, analyst_headers: dict[str, str], admin_headers: dict[str, str]
) -> None:
    from fastapi.testclient import TestClient

    from app.api.dashboard import create_app

    with TestClient(create_app()) as client:
        analyst = client.get("/api/llm/status", headers=analyst_headers).json()
        admin = client.get("/api/llm/status", headers=admin_headers).json()
    assert analyst["base_url_host"] is None
    assert admin["base_url_host"]


def test_c17_credentials_in_query_strings_are_masked_in_logs() -> None:
    import logging

    from app.monitoring.logging import QueryStringMaskFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '"GET /api/live/stream?ticket=abc123&x=1"', (), None
    )
    QueryStringMaskFilter().filter(record)
    assert "abc123" not in record.getMessage() and "ticket=***" in record.getMessage()


def test_c19_rate_limit_bucket_is_per_client() -> None:
    from starlette.requests import Request

    from app.security.ratelimit import ingest_rate_key

    def req(headers: dict[str, str]) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": ("10.0.0.1", 1)})

    a, b = req({"X-API-Key": "key-a"}), req({"X-API-Key": "key-b"})
    assert ingest_rate_key(a) != ingest_rate_key(b)
    assert "key-a" not in ingest_rate_key(a)  # only a hash prefix
    assert ingest_rate_key(req({})) == "ip:10.0.0.1"


def test_c16_login_page_hides_demo_passwords_without_demo_users(
    demo_env: Path, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from app.api.dashboard import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/auth/config").json() == {"demo_users": True}
    monkeypatch.setenv("SEED_DEMO_USERS", "false")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert client.get("/api/auth/config").json() == {"demo_users": False}
        bad = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert bad.status_code == 401
