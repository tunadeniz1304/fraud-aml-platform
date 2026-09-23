"""Tests for the streaming simulator and FastAPI dashboard surface."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.schemas import TransactionIn
from app.core.event_bus import EventBus
from app.core.stream_simulator import TransactionStreamSimulator


class TestStreamSimulator:
    async def test_emits_every_transaction(self, bus: EventBus):
        sim = TransactionStreamSimulator()
        seen = [t async for t in sim(bus)]
        assert len(seen) == 9
        assert all(t["transaction_id"] for t in seen)

    async def test_publishes_to_bus(self, bus: EventBus):
        from app.agents.transaction_monitor import TransactionMonitor
        monitor = TransactionMonitor(bus)
        sim = TransactionStreamSimulator(path=None)
        async for _ in sim(bus):
            pass
        assert len(monitor.monitored) == 9


class TestEventBusIsolation:
    async def test_failing_subscriber_does_not_block_others(self, bus):
        received = []

        def boom(payload):
            raise RuntimeError("subscriber exploded")

        async def record(payload):
            received.append(payload["k"])

        bus.subscribe("t", boom)
        bus.subscribe("t", record)
        await bus.publish("t", {"k": 1})  # must not raise
        assert received == [1]

    async def test_subscriber_can_raise_without_breaking_publisher(self, bus):
        async def boom(payload):
            raise ValueError("async boom")

        bus.subscribe("t", boom)
        # Second publish to the same bus still works.
        await bus.publish("t", {})
        await bus.publish("t", {})
        assert True


class TestSchemas:
    def test_invalid_amount_rejected(self):
        import pytest
        with pytest.raises(ValidationError):
            TransactionIn(
                transaction_id="TX-X",
                ts="2026-09-22T14:30:00",
                customer_id="C1",
                amount=-1,
                currency="TRY",
                device_id="D1",
                location="İstanbul",
            )

    def test_extra_fields_rejected(self):
        import pytest
        with pytest.raises(ValidationError):
            TransactionIn(
                transaction_id="TX-X",
                ts="2026-09-22T14:30:00",
                customer_id="C1",
                amount=10,
                currency="TRY",
                device_id="D1",
                location="İstanbul",
                hacked="x",
            )


class TestRiskExplain:
    def test_explain_defaults_empty(self):
        from app.core.risk_engine import RiskFactors, explain_factors
        empty = RiskFactors(amount=0.0, device=0.0, location=0.0, time=0.0, velocity=0.0)
        assert explain_factors(empty) == []

    def test_explain_weights_dominant_signals(self):
        from app.core.risk_engine import RiskFactors, explain_factors
        f = RiskFactors(amount=0.9, device=1.0, location=0.0, time=0.0, velocity=0.0)
        out = explain_factors(f)
        assert out[0] == "bilinmeyen cihaz"
        assert "tutar ortalamanın üzerinde" in out

    def test_country_score(self):
        from app.core.risk_engine import _country_score
        assert _country_score("NG") == 1.0
        assert _country_score("TR") == 0.0

    def test_peer_group_baseline(self):
        from app.agents.context_analyst import ContextAnalyst
        records = [
            {"customer_id": "A", "home_city": "İstanbul", "avg_amount": 2000},
            {"customer_id": "B", "home_city": "İstanbul", "avg_amount": 3000},
            {"customer_id": "C", "home_city": "Ankara", "avg_amount": 9000},
        ]
        a = records[0]
        assert ContextAnalyst._peer_avg(records, a) == 3000.0
        # No peers in group -> own average.
        c = records[2]
        assert ContextAnalyst._peer_avg(records, c) == 9000.0

    def test_microcluster_burst(self):
        from app.core.aggregates import MicroclusterDetector
        d = MicroclusterDetector(threshold=3)
        tx = {"customer_id": "C1", "beneficiary_id": "B9", "device_id": "D1"}
        # Graded ramp: 1/3, 2/3, then full 1.0 at threshold.
        assert abs(d.update(tx, 1000.0) - 1 / 3) < 1e-9
        assert abs(d.update(tx, 2000.0) - 2 / 3) < 1e-9
        assert abs(d.update(tx, 3000.0) - 1.0) < 1e-9
        # Events older than the window are pruned -> score resets toward 1/3.
        assert abs(d.update(tx, 10_000_000.0) - 1 / 3) < 1e-9

    def test_online_stats_welford(self):
        from app.core.aggregates import OnlineStats
        s = OnlineStats()
        for v in (10.0, 20.0, 30.0):
            s.update(v)
        assert s.n == 3
        assert abs(s.mean - 20.0) < 1e-9
        summ = s.summary()
        assert summ["n"] == 3
        assert "histogram" in summ

    def test_csv_export_contains_header_and_rows(self, store):
        import asyncio

        store.append_audit(
            transaction_id="TX-1", customer_id="C1", risk_score=0.9,
            decision="BLOKE", reason="test",
        )
        from app.api.admin import export_audit
        from app.api.state import state

        state.store = store
        try:
            text = asyncio.run(export_audit())
        finally:
            state.store = None
        assert '"id","created_at"' in text
        assert '"BLOKE"' in text
        assert '"TX-1"' in text


class TestAdminStore:
    def test_status_transition_and_audit(self, store):
        # Manual operator unblock: BLOKE -> AKTIF, with audit row.
        store.set_hesap_durumu("CUST-0001", "BLOKE")
        assert store.get_account("CUST-0001")["hesap_durumu"] == "BLOKE"
        store.set_hesap_durumu("CUST-0001", "AKTIF")
        assert store.get_account("CUST-0001")["hesap_durumu"] == "AKTIF"
        store.append_audit(
            transaction_id="MANUAL",
            customer_id="CUST-0001",
            risk_score=0.0,
            decision="STATUS:BLOKE->AKTIF",
            reason="Operatör manuel kaldırma",
        )
        audit = store.list_audit()
        assert audit[0]["decision"] == "STATUS:BLOKE->AKTIF"

    def test_invalid_status_rejected(self, store):
        import pytest
        with pytest.raises(ValueError):
            store.set_hesap_durumu("CUST-0001", "BOGUS")
