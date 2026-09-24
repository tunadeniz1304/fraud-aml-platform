"""Pipeline container: builds, starts and stops the whole detection stack.

Used by the FastAPI ``lifespan`` (bug #1) and the console runner.

Topology::

    Redis stream fraud:transaction.created   (EVENT_BUS=redis, ingress)
          │   consumer group "pipeline" (ack, retry, DLQ, idempotency)
          ▼
    InMemoryBus: created → monitored → analyzed → decision.made
          │                                           │
          ▼                                           ▼
    feature store (memory | Redis)       write-behind DB writer (+ audit chain)
                                          Redis stream fraud:decision.made (egress)

The internal stages stay in-process for latency; Redis Streams decouple
external producers (simulator, core banking) and consumers (worker).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.agents.action_agent import ActionAgent
from app.agents.context_analyst import ContextAnalyst
from app.agents.transaction_monitor import TransactionMonitor
from app.bus.memory import InMemoryBus
from app.bus.redis_streams import RedisStreamsBus
from app.cases.service import CaseService
from app.config import Settings, get_settings
from app.core.behavior_store import BehaviorStore
from app.core.stream_simulator import TransactionStreamSimulator
from app.db import repository as repo
from app.db.database import Database
from app.db.models import Decision
from app.db.writer import PersistenceWriter
from app.features.extractor import CustomerDirectory, FeatureExtractor
from app.features.store import FeatureStateStore, MemoryFeatureStore, RedisFeatureStore
from app.llm.config import LLMSettings
from app.llm.service import LLMService
from app.ml.registry import ModelRegistry
from app.scoring import rule_store
from app.scoring.engine import ScoringEngine
from app.scoring.rules import load_rule_file
from app.services.accounts import AccountService

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
        db: Database,
        writer: PersistenceWriter,
        accounts: AccountService,
        customers: CustomerDirectory,
        extractor: FeatureExtractor,
        bus: InMemoryBus,
        monitor: TransactionMonitor,
        analyst: ContextAnalyst,
        action: ActionAgent,
        llm: LLMService,
        ingress: RedisStreamsBus | None = None,
        redis: Any = None,
        vector: BehaviorStore | None = None,
        cases: CaseService | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.writer = writer
        self.accounts = accounts
        self.customers = customers
        self.extractor = extractor
        self.bus = bus
        self.monitor = monitor
        self.analyst = analyst
        self.action = action
        self.llm = llm
        self.ingress = ingress
        self.redis = redis
        self.vector = vector
        self.cases = cases or CaseService(
            db, accounts=accounts, writer=writer, customer_name=self._customer_name
        )
        self.results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._stream_task: asyncio.Task[None] | None = None
        self._vector_task: asyncio.Task[None] | None = None
        self.stream_done = asyncio.Event()
        bus.subscribe(ActionAgent.DECIDED, self._remember)
        bus.subscribe(ActionAgent.DECIDED, self._to_cases)
        bus.subscribe(TransactionMonitor.REJECTED, self._remember)
        if ingress is not None:
            ingress.subscribe(TransactionMonitor.CREATED, self._from_ingress, group="pipeline")
            bus.subscribe(ActionAgent.DECIDED, self._to_egress)

    # --- compatibility shim -------------------------------------------------------------
    @property
    def store(self) -> AccountService:
        return self.accounts

    @property
    def engine(self) -> ScoringEngine:
        return self.analyst.engine

    # --- bookkeeping -------------------------------------------------------------------
    def _remember(self, event: dict[str, Any]) -> None:
        tx_id = str(event.get("transaction_id", ""))
        if not tx_id:
            return
        self.results[tx_id] = event
        self.results.move_to_end(tx_id)
        while len(self.results) > self.settings.analyzed_buffer:
            self.results.popitem(last=False)

    def customer_names(self) -> list[str]:
        return self.customers.names()

    def _customer_name(self, customer_id: str) -> str:
        customer = self.customers.get(customer_id)
        return customer.name if customer is not None and customer.name else customer_id

    async def _to_cases(self, event: dict[str, Any]) -> None:
        """HOLD / BLOCK / case-required decisions become alerts grouped into cases."""
        try:
            await self.cases.on_decision(event)
        except Exception:  # case intake must never break the decision flow
            logger.exception("[Cases] alert/vaka oluşturulamadı (%s)", event.get("transaction_id"))

    async def _from_ingress(self, payload: dict[str, Any]) -> None:
        """Redis ingress: same strict validation as the HTTP API.

        A payload that fails validation raises, so the Redis bus retries it and
        finally parks it in the dead-letter stream (poison message).
        """
        from app.api.schemas import TransactionIn

        model = TransactionIn.model_validate(payload)
        tx = model.model_dump(mode="json", exclude_none=True)
        tx["ts"] = model.ts.isoformat()
        await self.ingest(tx)

    async def _to_egress(self, event: dict[str, Any]) -> None:
        assert self.ingress is not None
        slim = {
            k: event.get(k)
            for k in (
                "transaction_id",
                "customer_id",
                "ts",
                "amount_try",
                "beneficiary_id",
                "device_id",
                "risk_score",
                "decision",
                "decision_legacy",
            )
        }
        await self.ingress.publish(ActionAgent.DECIDED, slim, key=str(event["transaction_id"]))

    # --- lifecycle -------------------------------------------------------------------------
    async def feed(self, transactions: list[dict[str, Any]]) -> None:
        for tx in transactions:
            tx_id = str(tx.get("transaction_id") or "")
            await self.bus.publish(TransactionMonitor.CREATED, tx, key=tx_id or None)

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
            if not store.enabled:
                logger.warning("[Vector] RAG deposu devre dışı — copilot RAG'siz çalışacak")
                return
            await asyncio.to_thread(store.seed)
            self.vector = store
            logger.info("[Vector] RAG deposu hazır (%s gömme)", store.embedding)
        except Exception:
            logger.exception("[Vector] RAG deposu başlatılamadı — copilot RAG'siz çalışacak")

    async def start(self) -> None:
        await self.writer.start()
        await self.bus.start()
        if self.ingress is not None:
            await self.ingress.start()
            logger.info("Redis Streams ingress aktif (grup: pipeline)")
        if self.settings.vector_store == "chroma" and self.vector is None:
            self._vector_task = asyncio.create_task(self._init_vector(), name="vector-init")
        mode = self.settings.stream_mode
        if mode == "batch":
            await self.feed(load_transactions(self.settings.resolved_transactions_path))
            await self.drain()
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

    async def drain(self) -> None:
        """Wait until the bus is idle and every write is committed."""
        await self.bus.drain()
        await self.writer.flush()

    async def stop(self) -> None:
        for task in (self._stream_task, self._vector_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.ingress is not None:
            await self.ingress.stop()
        await self.bus.stop()
        await self.writer.stop()
        await self.extractor.store.close()
        await self.db.dispose()
        if self.redis is not None:
            with contextlib.suppress(Exception):
                await self.redis.aclose()

    # --- ingest ---------------------------------------------------------------------------------
    async def _already_decided(self, tx_id: str) -> bool:
        async with self.db.session() as session:
            row = await session.execute(
                select(Decision.id).where(Decision.transaction_id == tx_id).limit(1)
            )
            return row.scalar_one_or_none() is not None

    async def ingest(self, tx: dict[str, Any]) -> dict[str, Any] | None:
        """Publish one transaction and return its decision (idempotent per id)."""
        tx_id = str(tx.get("transaction_id") or "")
        if tx_id and tx_id in self.results:
            return {**self.results[tx_id], "duplicate": True}
        if tx_id and await self._already_decided(tx_id):
            return {"transaction_id": tx_id, "duplicate": True, "persisted_only": True}
        await self.bus.publish(TransactionMonitor.CREATED, tx, key=tx_id or None)
        if self.bus.running:
            await self.bus.drain()
        return self.results.get(tx_id)


def build_feature_store(settings: Settings, redis: Any) -> FeatureStateStore:
    if settings.resolved_feature_store == "redis":
        if redis is None:
            raise RuntimeError("FEATURE_STORE=redis ancak REDIS_URL tanımlı değil")
        return RedisFeatureStore(redis)
    return MemoryFeatureStore(settings.feature_max_entities)


async def build_pipeline(
    settings: Settings | None = None,
    *,
    llm_settings: LLMSettings | None = None,
    llm: LLMService | None = None,
    redis: Any = None,
) -> Pipeline:
    """Construct every component from configuration."""
    settings = settings or get_settings()
    llm_settings = llm_settings or LLMSettings()
    logger.info(llm_settings.startup_line())
    llm = llm or LLMService(llm_settings)  # LLM_MODE=live without key -> fail fast

    db = Database(settings.resolved_database_url)
    if settings.db_auto_create:
        await db.create_all()
    customers = CustomerDirectory.from_file(settings.resolved_customers_path)
    registry = ModelRegistry(settings.resolved_models_dir)
    async with db.transaction() as session:
        await repo.upsert_customers(session, customers.records())
        seeded = await rule_store.seed_rules(session, load_rule_file(settings.resolved_rules_path))
        await repo.upsert_models(session, registry.models())
    async with db.session() as session:
        ruleset = rule_store.build_ruleset(await rule_store.load_rules(session))
    logger.info(
        "[Rules] %d kural etkin (%s, %d yeni tohum)", len(ruleset.rules), ruleset.version, seeded
    )
    writer = PersistenceWriter(
        db,
        batch_size=settings.writer_batch_size,
        flush_interval=settings.writer_flush_ms / 1000,
        queue_size=settings.writer_queue_size,
    )
    accounts = AccountService(db, writer)
    await accounts.load()
    logger.info("[DB] %s hazır (%d müşteri)", db.safe_url, len(customers))

    if redis is None and settings.redis_url:
        import redis.asyncio as aioredis

        redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    extractor = FeatureExtractor(build_feature_store(settings, redis), customers)
    bus = InMemoryBus(
        max_retries=settings.bus_max_retries,
        partitions=settings.bus_partitions,
        queue_size=settings.bus_queue_size,
    )
    ingress = None
    if settings.event_bus == "redis":
        if redis is None:
            raise RuntimeError("EVENT_BUS=redis ancak REDIS_URL tanımlı değil")
        ingress = RedisStreamsBus(
            redis,
            prefix=settings.redis_stream_prefix,
            max_retries=settings.bus_max_retries,
            claim_idle_ms=settings.bus_claim_idle_ms,
        )
    engine = ScoringEngine.from_settings(extractor, ruleset=ruleset, registry=registry)
    logger.info(
        "[Scoring] champion=%s challenger=%s eşikler=%s",
        engine.model.version if engine.model else "-",
        engine.challenger.version if engine.challenger else "-",
        engine.policy.thresholds.as_dict(),
    )
    monitor = TransactionMonitor(bus)
    analyst = ContextAnalyst(bus, account_status=accounts.get_status, engine=engine)
    action = ActionAgent(bus, accounts=accounts, writer=writer, policy=engine.policy)
    return Pipeline(
        settings=settings,
        db=db,
        writer=writer,
        accounts=accounts,
        customers=customers,
        extractor=extractor,
        bus=bus,
        monitor=monitor,
        analyst=analyst,
        action=action,
        llm=llm,
        ingress=ingress,
        redis=redis,
    )
