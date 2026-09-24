"""Console simulation: feed the demo stream through the full pipeline.

    transaction.created -> TransactionMonitor -> transaction.monitored
                        -> ContextAnalyst      -> transaction.analyzed
                        -> ActionAgent         -> decision.made (+ audit)

Prints a report of each transfer's risk score and the autonomous decision.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from app.config import LOGS_DIR, get_settings
from app.monitoring.logging import configure_logging
from app.pipeline import build_pipeline, load_transactions

logger = logging.getLogger("fraud.main")


def build_report_rows(
    monitored: Iterable[dict[str, Any]],
    analyzed: Iterable[dict[str, Any]],
    blocked: Iterable[dict[str, Any]],
    warned: Iterable[dict[str, Any]],
    passed: Iterable[dict[str, Any]],
) -> list[tuple[str, str, float, str, float, str]]:
    """Join the stage outputs by transaction id in O(n) (fixes bug #15)."""
    risk = {a["transaction_id"]: float(a["risk_score"]) for a in analyzed}
    decision: dict[str, str] = {}
    for label, rows in (("geçti", passed), ("İNCELEME", warned), ("BLOKE (otonom)", blocked)):
        for row in rows:
            decision[row["transaction_id"]] = label
    out = []
    for m in monitored:
        txid = m["transaction_id"]
        out.append(
            (
                txid,
                m["customer_id"],
                float(m["amount"]),
                m.get("currency", ""),
                risk.get(txid, float("nan")),
                decision.get(txid, "analiz edilmedi"),
            )
        )
    return out


def print_report(monitor: Any, analyst: Any, action: Any) -> None:
    rows = build_report_rows(
        monitor.monitored, analyst.analyzed, action.blocked, action.warned, action.passed
    )
    print()
    print("=" * 78)
    print("FRAUD ANALİZ RAPORU — OTONOM AJAN PİPLİNİ")
    print("=" * 78)
    print(f"{'Transfer':<10}{'Müşteri':<12}{'Tutar':>12}  {'Risk':>6}  Karar")
    print("-" * 78)
    for txid, customer, amount, currency, risk, decision in rows:
        print(f"{txid:<10}{customer:<12}{amount:>10.2f} {currency:<3}  {risk:>6.2f}  {decision}")
    print("-" * 78)
    print(f"Toplam transfer : {len(monitor.monitored)}")
    print(f"Reddedilen      : {len(monitor.rejected)}")
    print(f"Bloke edilen    : {len(action.blocked)}")
    print(f"İncelemeye alınan: {len(action.warned)}")
    print(f"Akışa bırakılan : {len(action.passed)}")
    print("=" * 78)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings, log_file=LOGS_DIR / "fraud_agent.log")
    pipeline = await build_pipeline(settings)
    await pipeline.feed(load_transactions(settings.resolved_transactions_path))
    print_report(pipeline.monitor, pipeline.analyst, pipeline.action)
    await pipeline.stop()
    logger.info("Simülasyon tamamlandı")


if __name__ == "__main__":
    asyncio.run(main())
