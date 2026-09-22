"""End-to-end simulation / test flow for the fraud detection agent system.

Loads the mock incoming-transaction stream and feeds every transfer through the
event-driven pipeline:

    transaction.created -> TransactionMonitor -> transaction.monitored
                        -> ContextAnalyst      -> transaction.analyzed
                        -> ActionAgent         -> account.blocked (+ log)

Prints a human-readable report of each transfer's risk score and the agent's
autonomous decision (passed vs. account blocked)."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from app import config
from app.agents.action_agent import ActionAgent
from app.agents.context_analyst import ContextAnalyst
from app.agents.transaction_monitor import TransactionMonitor
from app.core.account_store import AccountStore
from app.core.behavior_store import BehaviorStore
from app.core.event_bus import EventBus
from app.llm import LLMClient

# --- Logging -----------------------------------------------------------------
config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = config.LOGS_DIR / "fraud_agent.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("fraud.main")


def load_transactions() -> list[dict]:
    path = config.DATA_DIR / "transactions.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


async def run_pipeline(bus: EventBus, transactions: list[dict]) -> None:
    """Feed the whole mock stream through the bus as created events."""
    for tx in transactions:
        await bus.publish(TransactionMonitor.CREATED, tx)


def print_report(monitor, analyst, action) -> None:
    print()
    print("=" * 78)
    print("FRAUD ANALİZ RAPORU — OTONOM AJAN PİPLİNİ")
    print("=" * 78)
    print(f"{'Transfer':<10}{'Müşteri':<12}{'Tutar':>12}  {'Risk':>6}  Karar")
    print("-" * 78)
    for monitored in monitor.monitored:
        txid = monitored["transaction_id"]
        block = next((b for b in action.blocked if b["transaction_id"] == txid), None)
        passed = next((p for p in action.passed if p["transaction_id"] == txid), None)
        analyzed = next((a for a in analyst.analyzed if a["transaction_id"] == txid), None)
        risk = analyzed["risk_score"] if analyzed else float("nan")
        if block:
            decision = "BLOKE (otonom)"
        elif passed:
            decision = "geçti"
        else:
            decision = "analiz edilmedi"
        print(
            f"{txid:<10}{monitored['customer_id']:<12}"
            f"{monitored['amount']:>10.2f} {monitored.get('currency', ''):<3}  "
            f"{risk:>6.2f}  {decision}"
        )
    print("-" * 78)
    print(f"Toplam transfer : {len(monitor.monitored)}")
    print(f"Analiz edilen   : {len(analyst.analyzed)}")
    print(f"Bloke edilen    : {len(action.blocked)}")
    print(f"Akışa bırakılan : {len(action.passed)}")
    print("=" * 78)

    if action.blocked:
        print("\nOtonom bloke edilen hesaplar:")
        for b in action.blocked:
            print(f"  - {b['transaction_id']} | müşteri {b['customer_id']} | "
                  f"risk {b['risk_score']:.2f} | {b['reason']}")


async def main() -> None:
    logger.info("Fraud analiz ajanı başlatılıyor (eşik=%.2f)", config.RISK_THRESHOLD)

    # Persistence: relational account/audit store + vector behaviour store.
    store = AccountStore()
    store.seed_accounts()
    vector = BehaviorStore()
    vector.seed()

    llm = LLMClient()

    bus = EventBus()
    monitor = TransactionMonitor(bus)
    analyst = ContextAnalyst(bus, behavior_store=vector, llm=llm)
    action = ActionAgent(bus, store=store)

    transactions = load_transactions()
    logger.info("%d işlem olay akışına besleniyor...", len(transactions))

    await run_pipeline(bus, transactions)

    print_report(monitor, analyst, action)
    store.close()
    logger.info("Simülasyon tamamlandı — log dosyası: %s", LOG_FILE)


if __name__ == "__main__":
    asyncio.run(main())
