"""End-to-end pipeline tests (Adım 5): monitor -> analyst -> action."""

from __future__ import annotations

from app.agents.action_agent import ActionAgent
from app.agents.context_analyst import ContextAnalyst
from app.agents.transaction_monitor import TransactionMonitor


async def run_pipeline(bus, transactions):
    for tx in transactions:
        await bus.publish(TransactionMonitor.CREATED, tx)


class TestPipeline:
    async def test_full_stream_publishing(self, bus, store, sample_transactions):
        monitor = TransactionMonitor(bus)
        analyst = ContextAnalyst(bus)
        action = ActionAgent(bus, store=store)
        await run_pipeline(bus, sample_transactions)

        assert len(monitor.monitored) == len(sample_transactions)
        assert all(m["well_formed"] for m in monitor.monitored)
        assert {a["transaction_id"] for a in analyst.analyzed} == {
            t["transaction_id"] for t in sample_transactions
        }

    async def test_anomalous_blocks_and_status(self, bus, store, sample_transactions):
        monitor = TransactionMonitor(bus)
        ContextAnalyst(bus)
        action = ActionAgent(bus, store=store)
        await run_pipeline(bus, sample_transactions)

        # TX-B1 (CUST-0002, 90000 EUR, Berlin, unknown device) must be blocked.
        assert len(action.blocked) >= 1
        blocked_tx = next(b for b in action.blocked if b["transaction_id"] == "TX-B1")
        assert blocked_tx["risk_score"] >= 0.75

        # Account row must flip to BLOKE in SQLite.
        account = store.get_account("CUST-0002")
        assert account is not None
        assert account["hesap_durumu"] == "BLOKE"

    async def test_clean_transactions_pass(self, bus, store, sample_transactions):
        monitor = TransactionMonitor(bus)
        ContextAnalyst(bus)
        action = ActionAgent(bus, store=store)
        await run_pipeline(bus, sample_transactions[:2])

        assert len(action.blocked) == 0
        assert len(action.passed) == 2

    async def test_audit_log_written(self, bus, store, sample_transactions):
        TransactionMonitor(bus)
        ContextAnalyst(bus)
        ActionAgent(bus, store=store)
        await run_pipeline(bus, sample_transactions)

        audit = store.list_audit()
        assert len(audit) == len(sample_transactions)
        decisions = {row["decision"] for row in audit}
        assert "BLOKE" in decisions
