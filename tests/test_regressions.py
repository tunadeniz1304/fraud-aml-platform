"""Regression tests — one class per bug listed in Anil3.md §1 (#1 … #15)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI
from pydantic import SecretStr

from app import config
from app.agents.action_agent import ActionAgent
from app.agents.context_analyst import ContextAnalyst
from app.agents.transaction_monitor import TransactionMonitor
from app.api.dashboard import create_app
from app.api.frontend import render
from app.api.state import state
from app.config import get_settings
from app.core.account_state import next_status
from app.core.aggregates import MicroclusterDetector
from app.core.event_bus import EventBus
from app.llm.backends import OpenAICompatibleBackend
from app.llm.config import LLMSettings
from app.llm.service import LLMService
from app.llm.tasks import TransactionNarrative, narrate_transaction

CLEAN_TX = {
    "transaction_id": "TX-OK",
    "ts": "2026-09-22T14:30:00",
    "customer_id": "CUST-0001",
    "amount": 2000,
    "currency": "TRY",
    "device_id": "DEV-9F2A-11",
    "location": "İstanbul",
    "country": "TR",
    "purpose": "Market",
}


def wire(bus: EventBus, store=None, **analyst_kw):
    monitor = TransactionMonitor(bus)
    analyst = ContextAnalyst(bus, account_status=store.get_status if store else None, **analyst_kw)
    action = ActionAgent(
        bus,
        accounts=store.accounts if store else None,
        writer=store.writer if store else None,
    )
    return monitor, analyst, action


# --- #1 -------------------------------------------------------------------------
class TestBug01LifespanBuildsPipeline:
    def test_uvicorn_style_app_serves_data(self, app_env, analyst_headers):
        with TestClient(create_app()) as client:
            r = client.get("/api/status", headers=analyst_headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["analyzed"] == 9 and body["llm_mode"] == "demo"
            assert client.get("/api/health/ready").status_code == 200
        assert not state.wired  # detached on shutdown


# --- #2 -------------------------------------------------------------------------
class TestBug02StreamModeRunsInBackground:
    def test_server_up_before_stream_finishes(self, app_env, monkeypatch, analyst_headers):
        monkeypatch.setenv("STREAM_MODE", "stream")
        monkeypatch.setenv("STREAM_RATE", "40")
        get_settings.cache_clear()
        with TestClient(create_app()) as client:
            pipeline = state.pipeline
            assert not pipeline.stream_done.is_set()  # startup did not wait
            assert client.get("/api/health").status_code == 200
            deadline = time.monotonic() + 10
            while not pipeline.stream_done.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert pipeline.stream_done.is_set()
            assert client.get("/api/status", headers=analyst_headers).json()["analyzed"] == 9


# --- #3 -------------------------------------------------------------------------
class TestBug03DashboardApiInterpolation:
    def test_api_base_is_escaped_into_meta(self):
        html = render('https://api.example"><script>x</script>')
        assert '" + API + r"' not in html and "` + API + r`" not in html
        assert 'content="https://api.example&quot;&gt;&lt;script&gt;x&lt;/script&gt;"' in html

    def test_no_inline_script_or_style(self):
        html = render("")
        assert "<style" not in html and " style=" not in html
        for tag in html.split("<script")[1:]:
            assert tag.lstrip().startswith("src="), "inline script bulundu"

    def test_js_reads_meta_and_sends_token(self):
        js = (config.BASE_DIR / "app/static/legacy/dashboard.js").read_text(encoding="utf-8")
        assert 'meta[name="api-base"]' in js
        assert "Authorization" in js and "/api/admin/accounts" in js


# --- #4 -------------------------------------------------------------------------
class TestBug04MonotonicStateMachine:
    def test_system_never_downgrades(self):
        assert next_status("BLOKE", "INCELENIYOR") == "BLOKE"
        assert next_status("INCELENIYOR", "AKTIF") == "INCELENIYOR"
        assert next_status("AKTIF", "INCELENIYOR") == "INCELENIYOR"
        assert next_status("BLOKE", "AKTIF", actor="analyst") == "AKTIF"

    async def test_warning_does_not_downgrade_blocked_account(self, bus, store):
        await store.set_status("CUST-0001", "BLOKE")
        action = ActionAgent(bus, accounts=store.accounts, writer=store.writer)
        await bus.publish(
            ActionAgent.ANALYZED,
            {"transaction_id": "TX-W", "customer_id": "CUST-0001", "risk_score": 0.6},
        )
        assert len(action.warned) == 1
        assert store.get_status("CUST-0001") == "BLOKE"
        assert await store.db_status("CUST-0001") == "BLOKE"


# --- #5 -------------------------------------------------------------------------
class TestBug05BlockedAccountStoppedBeforeScoring:
    async def test_blocked_account_not_scored(self, bus, store):
        await store.set_status("CUST-0001", "BLOKE")
        _, analyst, action = wire(bus, store)
        await bus.publish(TransactionMonitor.CREATED, dict(CLEAN_TX))
        analyzed = analyst.analyzed[-1]
        assert analyzed["account_blocked"] is True and analyzed["scored"] is False
        assert analyst.stats.n == 0  # no scoring happened
        assert action.blocked[-1]["transaction_id"] == "TX-OK"
        assert "zaten BLOKE" in action.blocked[-1]["reason"]


# --- #6 -------------------------------------------------------------------------
class TestBug06AllSignalsReachTheScore:
    async def _score(self, **overrides) -> dict:
        bus = EventBus()
        _, analyst, _ = wire(bus)
        tx = {**CLEAN_TX, "transaction_id": "TX-S", **overrides}
        await bus.publish(TransactionMonitor.CREATED, tx)
        return analyst.analyzed[-1]

    async def test_each_context_signal_raises_the_score(self):
        base = (await self._score())["risk_score"]
        assert (await self._score(country="NG"))["risk_score"] > base
        assert (await self._score(purpose="ACİL: güvenli hesaba aktar"))["risk_score"] > base
        assert (await self._score(ip_address="45.84.1.10"))["risk_score"] > base  # VPN
        assert (await self._score(ip_address="197.210.1.1"))["risk_score"] > base  # NG IP, TR tx
        assert (await self._score(channel="atm"))["risk_score"] > base

    async def test_sanctions_hit_forces_review_regardless_of_score(self, bus, store, tmp_path):
        customers = tmp_path / "c.json"
        customers.write_text(
            json.dumps(
                [
                    {
                        "customer_id": "CUST-0001",
                        "name": "Viktor Melnikov",
                        "home_city": "İstanbul",
                        "known_device_ids": ["DEV-9F2A-11"],
                        "known_locations": ["İstanbul"],
                        "avg_amount": 2500,
                        "typical_hours": list(range(8, 20)),
                    }
                ]
            ),
            encoding="utf-8",
        )
        _, analyst, action = wire(bus, store, customers_path=customers)
        await bus.publish(TransactionMonitor.CREATED, dict(CLEAN_TX))
        assert analyst.analyzed[-1]["risk_score"] < get_settings().warning_threshold
        assert analyst.analyzed[-1]["sanctions_hit"] is True
        assert action.warned and "yaptırım" in action.warned[-1]["reason"]
        assert store.get_status("CUST-0001") == "INCELENIYOR"
        assert await store.db_status("CUST-0001") == "INCELENIYOR"

    def test_dead_settings_removed(self):
        fields = set(config.Settings.model_fields)
        assert "weight_semantic" not in fields and "max_amount_factor" not in fields


# --- #7 -------------------------------------------------------------------------
class TestBug07MalformedNotScored:
    async def test_malformed_published_as_rejected(self, bus, store):
        _, analyst, action = wire(bus, store)
        rejected: list[dict] = []
        bus.subscribe(TransactionMonitor.REJECTED, rejected.append)
        await bus.publish(TransactionMonitor.CREATED, {**CLEAN_TX, "amount": -5})
        await bus.publish(
            TransactionMonitor.CREATED, {**CLEAN_TX, "transaction_id": "TX-CCY", "currency": "XYZ"}
        )
        assert len(rejected) == 2 and not analyst.analyzed
        assert not (action.blocked or action.warned or action.passed)
        assert await store.audit() == []


# --- #8 -------------------------------------------------------------------------
class TestBug08UnknownCustomer:
    async def test_unknown_customer_goes_to_review_not_silent_block(self, bus, store, caplog):
        _, analyst, action = wire(bus, store)
        with caplog.at_level(logging.WARNING):
            await bus.publish(TransactionMonitor.CREATED, {**CLEAN_TX, "customer_id": "CUST-9999"})
        analyzed = analyst.analyzed[-1]
        assert analyzed["unknown_customer"] is True
        assert analyzed["risk_score"] == get_settings().unknown_customer_risk < 1.0
        assert action.warned and not action.blocked
        assert "hesap kaydı yok" in caplog.text
        assert await store.get_account("CUST-9999") is None

    async def test_update_of_missing_account_raises(self, store):
        from app.services.accounts import AccountNotFoundError

        with pytest.raises(AccountNotFoundError):
            await store.accounts.escalate("CUST-9999", "BLOKE", reason="x")
        with pytest.raises(AccountNotFoundError):
            await store.set_status("CUST-9999", "BLOKE")


# --- #9 -------------------------------------------------------------------------
class TestBug09AsyncLLM:
    def test_analyst_has_no_llm_on_scoring_path(self, bus):
        analyst = ContextAnalyst(bus)
        assert not hasattr(analyst, "llm")

    def test_empty_key_does_not_crash(self):
        service = LLMService(LLMSettings(api_key=SecretStr("")))
        assert service.mode == "demo"

    async def test_live_call_does_not_block_event_loop(self):
        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.3)
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "summary": "yavaş ama async bir yanıt",
                                        "recommended_action": "pass",
                                        "confidence": 0.5,
                                    }
                                ),
                            },
                        }
                    ],
                },
            )

        settings = LLMSettings(api_key=SecretStr("test-key-0123456789ab"), mode="live")
        client = AsyncOpenAI(
            api_key="test-key-0123456789ab",
            base_url="https://llm.test/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow)),
        )
        service = LLMService(settings, live_backend=OpenAICompatibleBackend(settings, client))
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        result = await narrate_transaction(service, {"transaction_id": "T", "risk_score": 0.1})
        hb.cancel()
        assert result.llm_mode == "live" and isinstance(result.output, TransactionNarrative)
        assert ticks >= 5  # a blocking call would yield 0-1 ticks (tolerant under CI load)


# --- #10 ------------------------------------------------------------------------
class TestBug10AuthHardening:
    def test_admin_token_compared_in_constant_time(self, monkeypatch):
        import app.security.deps as deps

        calls: list[tuple[str, str]] = []
        real = deps.constant_time_equals

        def spy(a: str, b: str) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(deps, "constant_time_equals", spy)
        monkeypatch.setenv("ADMIN_TOKEN", "s3cret-token")
        client = TestClient(create_app())
        r = client.get("/api/admin/accounts", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401 and calls

    def test_ingest_requires_credentials(self):
        client = TestClient(create_app())
        assert client.post("/api/transactions", json=CLEAN_TX).status_code == 401


# --- #11 ------------------------------------------------------------------------
class TestBug11BoundedMemory:
    async def test_buffers_are_bounded(self, bus):
        from app.features.extractor import CustomerDirectory, FeatureExtractor
        from app.features.store import MemoryFeatureStore

        directory = CustomerDirectory.from_file(get_settings().resolved_customers_path)
        store = MemoryFeatureStore(max_entities=3)
        _, analyst, _ = wire(bus, buffer=50, extractor=FeatureExtractor(store, directory))
        for i in range(120):
            await bus.publish(
                TransactionMonitor.CREATED,
                {
                    **CLEAN_TX,
                    "transaction_id": f"TX-{i}",
                    "customer_id": f"CUST-000{1 + i % 5}",
                    "beneficiary_id": f"B-{i}",
                    "device_id": f"D-{i}",
                },
            )
        assert len(analyst.analyzed) == 50
        sizes = store.size()
        assert sizes["customers"] <= 3 and sizes["devices"] <= 3 and sizes["payees"] <= 3

    def test_microcluster_edges_are_capped_and_swept(self):
        mc = MicroclusterDetector(window_seconds=10, max_edges=100, sweep_every=50)
        for i in range(500):
            mc.update({"customer_id": f"C{i}", "beneficiary_id": "B"}, i * 1_000.0)
        assert len(mc) <= 100
        mc.update({"customer_id": "late", "beneficiary_id": "B"}, 10_000_000.0)
        for i in range(49):
            mc.update({"customer_id": "late", "beneficiary_id": "B"}, 10_000_000.0 + i)
        assert len(mc) == 1  # every stale edge swept


# --- #12 ------------------------------------------------------------------------
class TestBug12CurrencyNormalisation:
    async def test_eur_amount_normalised_to_try(self, bus):
        _, analyst, _ = wire(bus)
        await bus.publish(
            TransactionMonitor.CREATED, {**CLEAN_TX, "amount": 1000, "currency": "EUR"}
        )
        eur = analyst.analyzed[-1]
        assert eur["amount_try"] == 45_000.0
        # F3: the v1 factor breakdown is gone; the TRY-normalised amount now
        # drives the feature store (45k TRY vs 2.5k TRY average).
        assert eur["features"]["amount_ratio"] > 10
        await bus.publish(
            TransactionMonitor.CREATED,
            {**CLEAN_TX, "transaction_id": "TX-T", "amount": 1000, "currency": "TRY"},
        )
        assert analyst.analyzed[-1]["features"]["amount_ratio"] < 1.0
        assert analyst.analyzed[-1]["risk_score"] < eur["risk_score"]


# --- #13 ------------------------------------------------------------------------
class TestBug13EnrichScriptUsesTargetDir:
    def test_script_never_touches_repo_data(self, tmp_path):
        repo_file = config.DATA_DIR / "transactions.json"
        before = repo_file.read_bytes()
        shutil.copy(repo_file, tmp_path / "transactions.json")
        cmd = [
            sys.executable,
            str(config.BASE_DIR / "scripts" / "enrich_data.py"),
            "--data-dir",
            str(tmp_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, cwd=config.BASE_DIR)
        first = (tmp_path / "transactions.json").read_bytes()
        subprocess.run(cmd, check=True, capture_output=True, cwd=config.BASE_DIR)
        assert (tmp_path / "transactions.json").read_bytes() == first
        assert repo_file.read_bytes() == before


# --- #14 ------------------------------------------------------------------------
class TestBug14RealRetrieval:
    def test_query_sentence_has_no_id_or_timestamp_noise(self):
        from app.core.behavior_store import transaction_document

        doc = transaction_document({**CLEAN_TX, "transaction_id": "TX-UNIQUE-777"})
        assert "TX-UNIQUE-777" not in doc and "2026-09-22" not in doc and "14:30" not in doc

    async def test_retrieved_document_reaches_the_llm(self, tmp_path):
        from app.core.behavior_store import BehaviorStore

        store = BehaviorStore(vector_dir=tmp_path / "chroma", embedding="hash")
        assert store.seed() == 5
        hit = store.retrieve({**CLEAN_TX})
        assert hit is not None and "İstanbul" in hit.document
        service = LLMService(LLMSettings(mode="demo"))
        result = await narrate_transaction(
            service, {**CLEAN_TX, "risk_score": 0.2}, behaviour_context=hit.document
        )
        assert "olağan davranışıyla" in result.output.summary
        assert hit.document[:40] in result.output.summary


# --- #15 ------------------------------------------------------------------------
class TestBug15ReportLockCsp:
    def test_report_join_is_linear(self):
        from main import build_report_rows

        n = 20_000
        rows = [{"transaction_id": f"T{i}", "customer_id": "C", "amount": 1.0} for i in range(n)]
        analyzed = [{"transaction_id": f"T{i}", "risk_score": 0.1} for i in range(n)]
        started = time.perf_counter()
        report = build_report_rows(rows, analyzed, [], [], analyzed)
        assert time.perf_counter() - started < 1.0
        assert len(report) == n and report[-1][5] == "geçti"

    async def test_concurrent_writers_keep_audit_chain_intact(self, store):
        """Many concurrent producers + batching writer: no lost rows, chain valid."""
        from app.db.audit import AuditEntry, verify_chain

        await store.writer.start()

        async def producer(k: int) -> None:
            for i in range(100):
                await store.writer.record_audit(
                    AuditEntry(
                        event_type="DECISION",
                        transaction_id=f"T{k}-{i}",
                        customer_id="CUST-0001",
                        risk_score=0.1,
                        decision="GECTI",
                    )
                )

        await asyncio.gather(*(producer(k) for k in range(8)))
        await store.set_status("CUST-0001", "BLOKE")  # synchronous writer path too
        assert len(await store.audit("DECISION")) == 800
        async with store.db.session() as session:
            assert (await verify_chain(session)).ok

    def test_csp_has_no_unsafe_inline(self):
        r = TestClient(create_app()).get("/")
        csp = r.headers["content-security-policy"]
        assert "unsafe-inline" not in csp and "script-src 'self'" in csp
