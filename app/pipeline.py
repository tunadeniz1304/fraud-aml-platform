"""Pipeline container: builds, starts and stops the whole detection stack.

Used by the FastAPI ``lifespan`` (fixes bug #1: the pipeline is now built in
every deployment, including ``uvicorn server:app`` inside Docker) and by the
console runner. In ``stream`` mode the simulator runs as a background task so
the HTTP server is up immediately (fixes bug #2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.agents.action_agent import ActionAgent
from app.agents.context_analyst import ContextAnalyst
from app.agents.transaction_monitor import TransactionMonitor
from app.config import Settings, get_settings
from app.core.account_store import AccountStore
from app.core.behavior_store import BehaviorStore
from app.core.event_bus import EventBus
from app.core.stream_simulator import TransactionStreamSimulator
from app.llm.config import LLMSettings
from app.llm.service import LLMService

logger = logging.getLogger("fraud.pipeline")


def load_transactions(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else [data]


class Pipeline:
    """Live references to every pipeline component."""

    def __init__(
        self,
        *,
        settings: Settings,
        bus: EventBus,
        store: AccountStore,
        monitor: TransactionMonitor,
        analyst: ContextAnalyst,
        action: ActionAgent,
        llm: LLMService,
        vector: BehaviorStore | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.store = store
        self.monitor = monitor
        self.analyst = analyst
        self.action = action
        self.llm = llm
        self.vector = vector
        self.results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._stream_task: asyncio.Task[None] | None = None
        self._vector_task: asyncio.Task[None] | None = None
        self.stream_done = asyncio.Event()
        bus.subscribe(ActionAgent.DECIDED, self._remember)
        bus.subscribe(TransactionMonitor.REJECTED, self._remember)

    # --- bookkeeping -----------------------------------------------------------
    def _remember(self, event: dict[str, Any]) -> None:
        tx_id = str(event.get("transaction_id", ""))
        if not tx_id:
            return
        self.results[tx_id] = event
        self.results.move_to_end(tx_id)
        while len(self.results) > self.settings.analyzed_buffer:
            self.results.popitem(last=False)

    def customer_names(self) -> list[str]:
        return [str(c.get("name", "")) for c in self.analyst.customers.values()]

    # --- lifecycle ---------------------------------------------------------------
    async def feed(self, transactions: list[dict[str, Any]]) -> None:
        for tx in transactions:
            await self.bus.publish(TransactionMonitor.CREATED, tx)

    async def _run_stream(self) -> None:
        simulator = TransactionStreamSimulator(
            self.settings.resolved_transactions_path,
            rate_per_second=self.settings.stream_rate,
            drift_scale=self.settings.stream_drift,
        )
        try:
            await simulator.run(self.bus)
            logger.info("Akış modu: simülatör tamamlandı")
        except asyncio.CancelledError:
            logger.info("Akış modu: simülatör durduruldu")
            raise
        finally:
            self.stream_done.set()

    async def _init_vector(self) -> None:
        """Build the Chroma RAG store off the startup path (model download etc.)."""
        try:
            store = await asyncio.to_thread(BehaviorStore, self.settings.resolved_customers_path)
            await asyncio.to_thread(store.seed)
            self.vector = store
            logger.info("[Vector] RAG deposu hazır (%s gömme)", store.embedding)
        except Exception:
            logger.exception("[Vector] RAG deposu başlatılamadı — copilot RAG'siz çalışacak")

    async def start(self) -> None:
        if self.settings.vector_store == "chroma" and self.vector is None:
            self._vector_task = asyncio.create_task(self._init_vector(), name="vector-init")
        mode = self.settings.stream_mode
        if mode == "batch":
            await self.feed(load_transactions(self.settings.resolved_transactions_path))
            self.stream_done.set()
            logger.info(
                "Pipeline hazır: %d işlem analiz edildi, %d bloke",
                len(self.analyst.analyzed),
                len(self.action.blocked),
            )
        elif mode == "stream":
            self._stream_task = asyncio.create_task(self._run_stream(), name="stream-simulator")
            logger.info(
                "Akış modu: simülatör arka planda başladı (%.1f işlem/sn)",
                self.settings.stream_rate,
            )
        else:
            self.stream_done.set()

    async def stop(self) -> None:
        for task in (self._stream_task, self._vector_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.store.close()

    async def ingest(self, tx: dict[str, Any]) -> dict[str, Any] | None:
        """Publish one transaction and return its decision (or rejection)."""
        await self.bus.publish(TransactionMonitor.CREATED, tx)
        return self.results.get(str(tx.get("transaction_id")))


async def build_pipeline(
    settings: Settings | None = None,
    *,
    llm_settings: LLMSettings | None = None,
    llm: LLMService | None = None,
) -> Pipeline:
    """Construct every component from configuration (no I/O beyond local files)."""
    settings = settings or get_settings()
    llm_settings = llm_settings or LLMSettings()
    logger.info(llm_settings.startup_line())
    llm = llm or LLMService(llm_settings)  # LLM_MODE=live without key -> fail fast

    store = AccountStore(settings.resolved_db_path)
    store.seed_accounts(settings.resolved_customers_path)

    bus = EventBus()
    monitor = TransactionMonitor(bus)
    analyst = ContextAnalyst(bus, settings.resolved_customers_path, account_status=store.get_status)
    action = ActionAgent(bus, store=store)
    return Pipeline(
        settings=settings,
        bus=bus,
        store=store,
        monitor=monitor,
        analyst=analyst,
        action=action,
        llm=llm,
    )
