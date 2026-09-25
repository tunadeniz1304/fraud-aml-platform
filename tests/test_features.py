"""Tests for the streaming simulator and FastAPI dashboard surface."""

from __future__ import annotations

import pytest
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


class TestSanctionsIntegration:
    async def test_analyst_flags_sanctioned_customer(self, bus, store, tmp_path):
        import json

        from app.agents.action_agent import ActionAgent
        from app.agents.context_analyst import ContextAnalyst
        from app.agents.transaction_monitor import TransactionMonitor

        # A customer whose name is on the PEP list (Hasan Abadi).
        cust_file = tmp_path / "customers_pep.json"
        cust_file.write_text(
            json.dumps(
                [
                    {
                        "customer_id": "CUST-PEP1",
                        "name": "Hasan Abadi",
                        "home_city": "Tahran",
                        "home_country": "IR",
                        "known_device_ids": ["DEV-PEP-1"],
                        "known_locations": ["Tahran"],
                        "avg_amount": 2000,
                        "typical_hours": [9, 10, 11, 12],
                    }
                ]
            ),
            encoding="utf-8",
        )

        analyst = ContextAnalyst(bus, customers_path=cust_file)
        ActionAgent(bus, accounts=store.accounts, writer=store.writer)
        TransactionMonitor(bus)
        tx = {
            "transaction_id": "TX-PEP-1",
            "ts": "2026-09-22T14:30:00",
            "customer_id": "CUST-PEP1",
            "amount": 5000,
            "currency": "TRY",
            "device_id": "DEV-PEP-1",
            "location": "Tahran",
            "country": "IR",
            "purpose": "Trade",
        }
        await bus.publish(TransactionMonitor.CREATED, tx)
        analyzed = analyst.analyzed[-1]
        assert analyzed["sanctions_hit"] is True
        assert analyzed["sanctions"][0]["id"] == "PEP-0001"


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


class TestIngestHardening:
    def test_control_chars_stripped(self):
        tx = TransactionIn(
            transaction_id="TX-\x1b[31mINJECTED",
            ts="2026-09-22T14:30:00",
            customer_id="C-1",
            amount=10,
            currency="TRY",
            device_id="D1",
            location="Lokasyon",
            purpose="Salary",
        )
        assert "\x1b" not in tx.transaction_id
        assert "\x1b" not in tx.purpose

    def test_amount_cap_rejects_absurd_values(self):
        import pytest

        with pytest.raises(ValidationError):
            TransactionIn(
                transaction_id="TX-X",
                ts="2026-09-22T14:30:00",
                customer_id="C1",
                amount=1e12,
                currency="TRY",
                device_id="D1",
                location="İstanbul",
            )

    def test_channel_enum_rejects_unknown(self):
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
                channel="usb",
            )

    def test_new_optional_fields_accepted(self):
        tx = TransactionIn(
            transaction_id="TX-X",
            ts="2026-09-22T14:30:00",
            customer_id="C1",
            amount=10,
            currency="TRY",
            device_id="D1",
            location="İstanbul",
            beneficiary_id="BEN-1",
            ip_address="88.241.10.5",
            channel="mobile",
        )
        assert tx.beneficiary_id == "BEN-1"
        assert tx.ip_address == "88.241.10.5"


class TestDriftInjection:
    async def test_drift_inflates_late_amounts(self, bus):
        sim = TransactionStreamSimulator(path=None, drift_scale=1.0, seed=1)
        seen = [t async for t in sim(bus)]
        assert len(seen) == 9
        assert seen[-1]["drift_scale"] == pytest.approx(1.0)
        assert seen[-1]["amount"] > seen[0]["amount"]

    async def test_no_drift_by_default(self, bus):
        sim = TransactionStreamSimulator()
        seen = [t async for t in sim(bus)]
        assert not any("drift_scale" in t for t in seen)


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

        assert _country_score("IR") == 1.0  # FATF call for action (data/jurisdictions)
        assert _country_score("UA") == 0.0  # not on the FATF lists
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

    async def test_csv_export_contains_header_and_rows(self, store):
        from app.api.admin import audit_csv
        from app.db.audit import AuditEntry

        await store.writer.record_audit(
            AuditEntry(
                event_type="DECISION",
                transaction_id="TX-1",
                customer_id="C1",
                risk_score=0.9,
                decision="BLOKE",
                reason='iç "tırnak", virgül',
            )
        )
        text = audit_csv(await store.audit())
        assert text.startswith('"id","created_at"')
        assert '"BLOKE"' in text and '"TX-1"' in text
        assert '"iç ""tırnak"", virgül"' in text  # RFC-4180 quoting


class TestAdminStore:
    async def test_status_transition_and_audit(self, store):
        # Manual analyst unblock: BLOKE -> AKTIF, each change audited.
        await store.set_status("CUST-0001", "BLOKE")
        assert await store.db_status("CUST-0001") == "BLOKE"
        await store.set_status("CUST-0001", "AKTIF")
        assert await store.db_status("CUST-0001") == "AKTIF"
        audit = await store.audit("ACCOUNT_STATUS")
        assert audit[0]["decision"] == "BLOKE->AKTIF"
        assert audit[0]["actor"] == "test"

    async def test_invalid_status_rejected(self, store):
        import pytest

        with pytest.raises(ValueError):
            await store.set_status("CUST-0001", "BOGUS")
