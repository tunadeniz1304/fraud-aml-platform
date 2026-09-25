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
@open_finding("A2")
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
@open_finding("A3")
async def test_a3_backfill_and_live_replay_produce_identical_features() -> None:
    from app.features.learning import should_learn  # single learning rule

    assert callable(should_learn)
    from app.ml.training import backfill, live_replay_features
    from app.scoring.rules import load_ruleset
    from app.synthetic.generator import SyntheticConfig, generate

    data = generate(SyntheticConfig(seed=7, customers=40, days=6))
    ruleset = load_ruleset()
    rows = await backfill(data.customers, data.transactions, ruleset)
    live = await live_replay_features(data.customers, data.transactions, ruleset)
    assert [r.features for r in rows] == live


# --- A4: rule floors must not force HOLD below the hold threshold for thin histories ----------
@open_finding("A4")
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
@open_finding("A5")
async def test_a5_card_testing_opens_card_testing_case(pipeline: Any) -> None:
    out = await pipeline.run_scenario("card_testing")
    assert out["cases"][0]["case_type"] == "CARD_TESTING"


# --- A6: graph direction + BLOCK feedback -----------------------------------------------------
@open_finding("A6")
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


@open_finding("A6")
async def test_a6_block_flags_graph_fraud_nodes(pipeline: Any) -> None:
    before = pipeline.graph.stats()["fraud_nodes"]
    out = await pipeline.run_scenario("ato")
    assert out["results"][0]["decision"] == "BLOCK"
    assert pipeline.graph.stats()["fraud_nodes"] > before


# --- A7: PSI must not alarm on the demo population at startup ---------------------------------
@open_finding("A7")
async def test_a7_no_drift_alarm_on_demo_startup(demo_env: Path, monkeypatch) -> None:
    from app.pipeline import build_pipeline

    monkeypatch.setenv("STREAM_MODE", "batch")
    get_settings.cache_clear()
    p = await build_pipeline(get_settings())
    await p.start()
    try:
        status = p.engine.drift.status()
        assert status["alerts"] == [], status["psi"]
    finally:
        await p.stop()


# --- B8: per-customer ordering ----------------------------------------------------------------
@open_finding("B8")
async def test_b8_concurrent_same_customer_velocity_is_exact(pipeline: Any) -> None:
    c = _customers()[7]
    now = datetime.now()
    txs = [
        _transfer(c, f"TX-CC-{i:04d}", now + timedelta(seconds=i), 50, beneficiary_id="TR-SAME")
        for i in range(50)
    ]
    results = await asyncio.gather(*(pipeline.ingest(t) for t in txs))
    counts = sorted(r["features"]["cnt_1h"] for r in results)
    assert counts == [float(i) for i in range(50)]


# --- B9: bounded Redis hashes -----------------------------------------------------------------
@open_finding("B9")
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
@open_finding("B10")
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


# --- B13: dynamic holiday calendar ------------------------------------------------------------
@open_finding("B13")
def test_b13_masak_deadline_skips_2027_and_2028_religious_holidays() -> None:
    from app.cases import sla

    # Ramazan Bayramı 2027: 9-11 March (Tue-Thu)
    assert sla.masak_deadline(datetime(2027, 3, 5, 10)).date().isoformat() == "2027-03-24"
    # Kurban Bayramı 2028: 5-8 May, then 19 May
    assert sla.masak_deadline(datetime(2028, 5, 3, 10)).date().isoformat() == "2028-05-22"


# --- B14: FATF-sourced high-risk list ---------------------------------------------------------
@open_finding("B14")
def test_b14_high_risk_countries_come_from_versioned_fatf_list() -> None:
    from app.core.jurisdictions import load_jurisdictions

    data = load_jurisdictions()
    assert data["source"].startswith("https://www.fatf-gafi.org/")
    assert data["as_of"]
    codes = get_settings().high_risk_country_set
    assert {"KP", "IR"} <= codes
    assert not {"UA", "AE"} & codes


# --- B15: sanctions blocking index + secondary keys -------------------------------------------
@open_finding("B15")
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


# --- C16: demo users only in dev --------------------------------------------------------------
@open_finding("C16")
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
@open_finding("C17")
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
@open_finding("C18")
def test_c18_metrics_not_public_by_default(demo_env: Path) -> None:
    from fastapi.testclient import TestClient

    from app.api.dashboard import create_app

    with TestClient(create_app()) as client:
        assert client.get("/metrics").status_code in (401, 403, 404)


# --- C19: sane ingest limit + HMAC nonce ------------------------------------------------------
@open_finding("C19")
def test_c19_ingest_limit_is_per_client_and_hmac_nonce_blocks_replay() -> None:
    from app.security.auth import verify_signature

    limit = int(get_settings().rate_limit_ingest.split("/")[0])
    assert limit <= 20_000
    import inspect

    assert "nonce" in inspect.signature(verify_signature).parameters


# --- C20: ASCII-folded / beneficiary names are redacted ---------------------------------------
@open_finding("C20")
def test_c20_redaction_handles_ascii_folding_and_beneficiaries() -> None:
    from app.llm.redaction import Redactor

    r = Redactor(["Ayşe Yılmaz"])
    text = r.redact("Ayse Yilmaz ve AYŞE YILMAZ, Selin Demirtaş'a gönderdi")
    assert "Ayse" not in text and "AYŞE" not in text
    payload = r.redact_obj({"beneficiary_name": "Kemal Sunal", "purpose": "Hakan Tas borcu"})
    assert "Kemal" not in json.dumps(payload, ensure_ascii=False)
    assert "Hakan" not in json.dumps(payload, ensure_ascii=False)


# --- C21: no internal hostnames in tracked files ----------------------------------------------
@open_finding("C21")
def test_c21_no_internal_llm_host_in_repository() -> None:
    out = subprocess.run(
        ["git", "grep", "-n", "ss" + "yz"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout.strip() == ""
