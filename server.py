"""FastAPI dashboard server.

Boots the real-time fraud pipeline (event bus + agents + stores) and binds it
to the dashboard REST API so the live status can be monitored over HTTP.

    python server.py            # uvicorn on 0.0.0.0:8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import uvicorn

from app import config
from app.agents.action_agent import ActionAgent
from app.agents.context_analyst import ContextAnalyst
from app.agents.transaction_monitor import TransactionMonitor
from app.api.dashboard import app, state
from app.core.account_store import AccountStore
from app.core.behavior_store import BehaviorStore
from app.core.event_bus import EventBus
from app.llm import LLMClient

logger = logging.getLogger("fraud.server")


def load_transactions() -> list[dict]:
    path = config.DATA_DIR / "transactions.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


async def feed_stream(bus: EventBus, transactions: list[dict]) -> None:
    for tx in transactions:
        await bus.publish(TransactionMonitor.CREATED, tx)


async def build_pipeline() -> None:
    """Construct the pipeline and expose it to the dashboard state."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    store = AccountStore()
    store.seed_accounts()
    vector = BehaviorStore()
    vector.seed()
    llm = LLMClient()

    bus = EventBus()
    monitor = TransactionMonitor(bus)
    analyst = ContextAnalyst(bus, behavior_store=vector, llm=llm)
    action = ActionAgent(bus, store=store)

    state.bus = bus
    state.monitor = monitor
    state.analyst = analyst
    state.action = action
    state.store = store
    state.vector = vector

    # Feed the historical stream so the dashboard shows real decisions.
    await feed_stream(bus, load_transactions())
    logger.info(
        "Pipeline hazır: %d işlem analiz edildi, %d bloke",
        len(analyst.analyzed), len(action.blocked),
    )


if __name__ == "__main__":
    asyncio.run(build_pipeline())
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
