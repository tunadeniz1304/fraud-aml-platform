"""End-to-end pipeline tests: monitor -> analyst -> action (+ persistence).

Updated for the async persistence layer (F2): account status comes from the
cached AccountService, DB rows are checked after the write-behind flush and the
audit log now also carries ACCOUNT_STATUS entries, so decision rows are counted
by ``event_type == "DECISION"``.
"""

from __future__ import annotations

from app.agents.action_agent import ActionAgent
from app.agents.context_analyst import ContextAnalyst
from app.agents.transaction_monitor import TransactionMonitor
from app.db.models import Decision, Transaction


async def run_pipeline(bus, transactions):
    for tx in transactions:
        await bus.publish(TransactionMonitor.CREATED, tx, key=tx["transaction_id"])


def wire(bus, store):
    monitor = TransactionMonitor(bus)
    analyst = ContextAnalyst(bus, account_status=store.get_status)
    action = ActionAgent(bus, accounts=store.accounts, writer=store.writer)
    return monitor, analyst, action


class TestPipeline:
    async def test_full_stream_publishing(self, bus, store, sample_transactions):
        monitor, analyst, _ = wire(bus, store)
        await run_pipeline(bus, sample_transactions)

        assert len(monitor.monitored) == len(sample_transactions)
        assert all(m["well_formed"] for m in monitor.monitored)
        assert {a["transaction_id"] for a in analyst.analyzed} == {
            t["transaction_id"] for t in sample_transactions
        }

    async def test_anomalous_blocks_and_status(self, bus, store, sample_transactions):
        _, _, action = wire(bus, store)
        await run_pipeline(bus, sample_transactions)

        # TX-B1 (CUST-0002, 90000 EUR, Berlin, unknown device) must be blocked.
        blocked_tx = next(b for b in action.blocked if b["transaction_id"] == "TX-B1")
        # BLOCK comes from the rule floor; the calibrated stacker score is ~0.5
        assert blocked_tx["decision"] == "BLOCK" and blocked_tx["risk_score"] > 0
        # Account flips to BLOKE in the cache immediately and in the DB after flush.
        assert store.get_status("CUST-0002") == "BLOKE"
        assert await store.db_status("CUST-0002") == "BLOKE"

    async def test_clean_transactions_pass(self, bus, store, sample_transactions):
        _, _, action = wire(bus, store)
        await run_pipeline(bus, sample_transactions[:2])

        assert len(action.blocked) == 0
        assert len(action.passed) == 2

    async def test_audit_log_written(self, bus, store, sample_transactions):
        wire(bus, store)
        await run_pipeline(bus, sample_transactions)

        decisions = await store.audit("DECISION")
        assert len(decisions) == len(sample_transactions)
        assert "BLOKE" in {row["decision"] for row in decisions}
        assert await store.count(Transaction) == len(sample_transactions)
        assert await store.count(Decision) == len(sample_transactions)
        status_rows = await store.audit("ACCOUNT_STATUS")
        assert any(r["decision"] == "AKTIF->BLOKE" for r in status_rows)
