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
