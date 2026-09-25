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
import os
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.agents.action_agent import ActionAgent
from app.agents.context_analyst import ContextAnalyst
from app.agents.transaction_monitor import TransactionMonitor
from app.bus.memory import InMemoryBus
from app.bus.redis_streams import RedisStreamsBus
from app.bus.writebehind import WriteBehindQueue
from app.cases.governance import DecisionLogicGovernance
from app.cases.outbox import CaseOutboxReplayer, needs_outbox
from app.cases.service import OPEN_STATUSES, ApprovalTx, CaseError, CaseService
from app.config import Settings, get_settings
from app.copilot.agent import CopilotAgent
from app.copilot.schemas import SibDraft
from app.copilot.templates import minimal_sib_draft
from app.copilot.tools import CopilotTools
from app.core.behavior_store import BehaviorStore
from app.core.stream_simulator import TransactionStreamSimulator
from app.db import repository as repo
from app.db.audit import AuditEntry
from app.db.database import Database
from app.db.models import Case, Decision, Label, Transaction
from app.db.writer import PersistenceWriter, WriteOp
from app.features.extractor import CustomerDirectory, FeatureExtractor
from app.features.store import FeatureStateStore, MemoryFeatureStore, RedisFeatureStore
from app.graph.entity_graph import EntityGraph
from app.idempotency import (
    IdempotencyConflict,
    IdempotencyIndex,
    IngestInProgress,
    check_digest,
    payload_digest,
)
from app.llm.config import LLMSettings
from app.llm.service import LLMService, OutputRejected
from app.ml.registry import ModelRegistry, RegistryError
from app.monitoring import metrics
from app.scenarios import ScenarioFactory
from app.scoring import rule_store
from app.scoring.engine import ScoringEngine
from app.scoring.policy import LEGACY, Thresholds
from app.scoring.rules import load_rule_file
from app.security.challenges import CHALLENGES, StepUpChallengeStore
from app.services.accounts import AccountService
from app.services.config_sync import ConfigSync

logger = logging.getLogger("fraud.pipeline")


def load_transactions(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else [data]


def rebase_timestamps(
    transactions: list[dict[str, Any]], *, end: datetime | None = None
) -> list[dict[str, Any]]:
    """Shift a warm-up history so its last event is (almost) now.

    The demo population ships with a fixed-date history; rebasing keeps the
    sliding windows, profiles and graph "recent" when the stack starts.
    """
    if not transactions:
        return transactions
    stamps = [datetime.fromisoformat(str(t["ts"])) for t in transactions]
    target = (end or datetime.now()).replace(microsecond=0) - timedelta(minutes=1)
    delta = target - max(stamps)
    return [
        {**t, "ts": (ts + delta).isoformat(timespec="seconds")}
        for t, ts in zip(transactions, stamps, strict=True)
    ]


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
        # A4: step-up challenges are issued on the shared decision path; with
        # Redis every worker (API, stream consumer) sees the same challenges
        self.challenges = StepUpChallengeStore(redis=redis) if redis is not None else CHALLENGES
        self.vector = vector
        self.cases = cases or CaseService(
            db,
            accounts=accounts,
            writer=writer,
            customer_name=self._customer_name,
            on_fraud_confirmed=self._fraud_confirmed,
            on_fraud_case=self._index_fraud_case,
            on_labelled=self._labelled,
        )
        self.copilot = CopilotAgent(
            llm,
            CopilotTools(
                db=db,
                cases=self.cases,
                customers=customers,
                sanctions=analyst.engine.sanctions,
                graph=analyst.engine.graph,
                vector=None,
                account_status=accounts.get_status,
                profile_lookup=self._profile_summary,
            ),
        )
        self._background: set[asyncio.Task[Any]] = set()
        self._enrich_slots = asyncio.Semaphore(max(1, settings.enrich_concurrency))
        self.idempotency = IdempotencyIndex(
            redis,
            ttl_s=settings.idempotency_ttl_s,
            pending_ttl_s=settings.idempotency_pending_ttl_s,
            pending_wait_s=settings.idempotency_pending_wait_ms / 1000,
        )
        # V6: only score + decision + idempotency record stay on the request
        # path; case intake, live fan-out and egress run write-behind.
        self.effects = WriteBehindQueue(queue_size=settings.write_behind_queue_size)
        self._live: set[asyncio.Queue[dict[str, Any]]] = set()
        self.recent_live: deque[dict[str, Any]] = deque(maxlen=2000)
        # H3: thresholds / rules / champion model are shared through the DB
        self.config = ConfigSync(db)
        self.cases.handlers["MODEL_PROMOTE"] = self._approve_promotion
        # A5: approval handlers publish inside the approval transaction
        self.governance = DecisionLogicGovernance(db, analyst.engine, writer, config=self.config)
        self.cases.handlers["RULE_CHANGE"] = self.governance.apply_rule_change
        self.cases.handlers["POLICY_THRESHOLDS"] = self.governance.apply_thresholds
        self.config.register("thresholds", self._apply_thresholds, restore=True)
        self.config.register("rules", self._apply_rules)
        self.config.register("models", self._apply_models)
        # M13: stale case-intake records are replayed from the DB outbox
        self.outbox = CaseOutboxReplayer(
            db,
            self.cases.on_decision,
            replay_s=settings.case_outbox_replay_s,
            on_case=lambda case_id, event: self._spawn(self._enrich_case(case_id, event)),
        )
        self.results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._inflight: dict[str, str] = {}  # tx_id -> payload digest (M1)
        self._stream_task: asyncio.Task[None] | None = None
        self._vector_task: asyncio.Task[None] | None = None
        self._ring_task: asyncio.Task[None] | None = None
        self._drift_task: asyncio.Task[None] | None = None
        self.rings: list[dict[str, Any]] = []
        self.stream_done = asyncio.Event()
        # A4: first, so a STEP_UP decided on any path (HTTP, stream, simulator)
        # has its one-time challenge before anyone can observe the decision
        bus.subscribe(ActionAgent.DECIDED, self._issue_step_up)
        bus.subscribe(ActionAgent.DECIDED, self._remember)
        # graph feedback is in-memory scoring state (like the feature-store
        # commit): it stays in decision order so the next transfer sees it
        bus.subscribe(ActionAgent.DECIDED, self._graph_feedback)
        bus.subscribe(ActionAgent.DECIDED, self.effects.submit)
        bus.subscribe(TransactionMonitor.REJECTED, self._remember)
        self.effects.subscribe(self._to_cases)
        self.effects.subscribe(self._to_live)
        if ingress is not None:
            ingress.subscribe(TransactionMonitor.CREATED, self._from_ingress, group="pipeline")
            self.effects.subscribe(self._to_egress)

    # --- compatibility shim -------------------------------------------------------------
    @property
    def store(self) -> AccountService:
        return self.accounts

    @property
    def engine(self) -> ScoringEngine:
        return self.analyst.engine

    @property
    def graph(self) -> EntityGraph | None:
        return self.analyst.engine.graph

    # --- graph feedback / rings ----------------------------------------------------------
    def _fraud_confirmed(self, customer_id: str, beneficiaries: list[str]) -> None:
        """Analyst-confirmed fraud propagates risk through the entity graph."""
        hub = self.engine.consortium
        if hub is not None:  # share hashed identifiers with the consortium
            from app.consortium import publish_fraud

            publish_fraud(hub, beneficiaries, [])
        graph = self.graph
        if graph is None:
            return
        graph.flag_customer(customer_id)
        for account in beneficiaries:
            graph.flag_account(account)

    async def issue_step_up(self, event: dict[str, Any]) -> str | None:
        """The one-time challenge id of a ``STEP_UP`` decision (idempotent: the
        still-valid one when it was already issued). Kept out of the decision
        event itself, which also reaches the live feed and the LLM explainer."""
        if event.get("decision") != "STEP_UP" or not event.get("transaction_id"):
            return None
        challenge = await self.challenges.issue(
            str(event["transaction_id"]), str(event.get("customer_id") or "")
        )
        return challenge.challenge_id if challenge is not None else None

    async def _issue_step_up(self, event: dict[str, Any]) -> None:
        try:
            await self.issue_step_up(event)
        except Exception:  # the channel can still get it via a replayed ingest
            logger.exception("[StepUp] %s için doğrulama üretilemedi", event.get("transaction_id"))

    def _graph_feedback(self, event: dict[str, Any]) -> None:
        """A scored BLOCK marks the counterparties (payee account, device) as fraud
        so risk propagates to whoever touches them next. The customer node is
        flagged only by an analyst's fraud label: in a takeover the customer is
        the victim."""
        graph = self.graph
        if graph is None or event.get("decision") != "BLOCK" or event.get("account_blocked"):
            return
        if not event.get("scored", True):
            return
        graph.flag_account(str(event.get("beneficiary_iban") or event.get("beneficiary_id") or ""))
        graph.flag_device(str(event.get("device_id") or ""))

    async def _labelled(self, tx_ids: list[str], feedback: str) -> None:
        """Analyst labels are profile feedback: a clean label teaches the profile."""
        for tx_id in tx_ids:
            await self.engine.apply_feedback(tx_id, "clean" if feedback == "clean" else "fraud")

    async def step_up_result(
        self,
        tx_id: str,
        *,
        success: bool,
        actor: str,
        expected_customer: str | None = None,
    ) -> dict[str, Any]:
        """Outcome of the step-up challenge (OTP) of a STEP_UP decision.

        A passed challenge proves the customer made the payment, so the new
        device and payee are learned as verified; a failed one teaches nothing
        and is audited. ``expected_customer`` is the customer the one-time
        challenge was issued for (HTTP path); a mismatch is refused.
        """
        event = self.results.get(tx_id)
        if event is None:
            stored = await self.stored_result(tx_id)
            if stored is None:
                raise KeyError(tx_id)
            event = stored
        if event.get("decision") != "STEP_UP":
            raise ValueError(f"{tx_id} için step-up istenmedi (karar {event.get('decision')})")
        if expected_customer is not None and str(event.get("customer_id")) != expected_customer:
            raise ValueError(f"{tx_id} için step-up doğrulaması başka bir müşteriye ait")
        learned = await self.engine.apply_feedback(
            tx_id, "step_up_passed" if success else "step_up_failed"
        )
        await self.writer.record_audit(
            AuditEntry(
                event_type="STEP_UP_RESULT",
                entity_type="transaction",
                entity_id=tx_id,
                transaction_id=tx_id,
                actor=actor,
                reason="OTP doğrulandı" if success else "OTP başarısız",
                customer_id=str(event.get("customer_id") or ""),
                payload={"success": success, "profile_learned": learned},
            )
        )
        metrics.STEP_UP_RESULTS.labels(outcome="passed" if success else "failed").inc()
        return {"transaction_id": tx_id, "success": success, "profile_learned": learned}

    async def load_graph_flags(self) -> int:
        """Re-flag nodes of analyst-labelled fraud (restart safe)."""
        graph = self.graph
        if graph is None:
            return 0
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(
                        Transaction.customer_id,
                        Transaction.beneficiary_iban,
                        Transaction.beneficiary_id,
                    )
                    .join(Label, Label.transaction_id == Transaction.id)
                    .where(Label.label == 1)
                )
            ).all()
        for customer_id, iban, beneficiary_id in rows:
            graph.flag_customer(customer_id)
            for account in (iban, beneficiary_id):
                if account:
                    graph.flag_account(account)
        return len(rows)

    async def refresh_rings(self) -> list[dict[str, Any]]:
        """Louvain ring detection on the live graph; persisted to ``graph_rings``."""
        graph = self.graph
        if graph is None:
            return []
        rings = await asyncio.to_thread(graph.detect_rings)
        if rings:
            async with self.db.transaction() as session:
                await repo.upsert_rings(session, rings)
                for ring in rings:
                    await session.execute(
                        update(Case)
                        .where(
                            Case.customer_id.in_(ring["members"]),
                            Case.ring_id.is_(None),
                            Case.status.in_(OPEN_STATUSES),
                        )
                        .values(ring_id=ring["id"])
                    )
        self.rings = rings
        return rings

    async def _ring_loop(self) -> None:
        interval = self.settings.ring_detect_interval_s
        while True:
            await asyncio.sleep(interval)
            try:
                rings = await self.refresh_rings()
                if rings:
                    logger.info(
                        "[Graph] %d halka tespit edildi: %s",
                        len(rings),
                        rings[0]["stats"]["summary"],
                    )
            except Exception:  # periodic analytics must never stop the pipeline
                logger.exception("[Graph] halka tespiti başarısız")

    async def run_scenario(self, name: str, customer_id: str | None = None) -> dict[str, Any]:
        """Inject an attack scenario and report the decisions of its key transfers."""
        factory = ScenarioFactory(self.customers, account_status=self.accounts.get_status)
        scenario = factory.build(name, customer_id)
        results: dict[str, dict[str, Any]] = {}
        for tx in scenario.transactions:
            result = await self.ingest(tx)
            if result is not None:
                results[tx["transaction_id"]] = result
        await self.drain()
        await self.wait_background()
        rings = await self.refresh_rings() if name == "mule_ring" else []
        key = []
        for tx_id in scenario.key_ids:
            r = results.get(tx_id) or {}
            key.append(
                {
                    "transaction_id": tx_id,
                    "customer_id": r.get("customer_id"),
                    "decision": r.get("decision"),
                    "risk_score": r.get("risk_score"),
                    "reasons": [x.get("text") for x in (r.get("reason_codes") or [])[:3]],
                    "ring_id": r.get("ring_id"),
                    "app_warning": r.get("app_warning"),
                }
            )
        cases = []
        for cid in dict.fromkeys(x["customer_id"] for x in key if x["customer_id"]):
            cases += await self.cases.list_cases(customer_id=str(cid), limit=5)
        for ring in dict.fromkeys(str(x["ring_id"]) for x in key if x.get("ring_id")):
            known = {c["id"] for c in cases}
            cases += [
                c
                for c in await self.cases.list_cases(ring_id=ring, limit=5)
                if c["id"] not in known
            ]
        return {
            **scenario.as_dict(),
            "results": key,
            "decisions": {tid: r.get("decision") for tid, r in results.items()},
            "cases": cases,
            "rings": [r for r in rings if set(r["members"]) & set(scenario.customers)],
        }

    # --- bookkeeping -------------------------------------------------------------------
    def _remember(self, event: dict[str, Any]) -> None:
        tx_id = str(event.get("transaction_id", ""))
        if not tx_id:
            return
        self.results[tx_id] = event
        self.results.move_to_end(tx_id)
        self.idempotency.add(tx_id)
        waiter = self._waiters.pop(tx_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(event)
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
            case_id = await self.cases.on_decision(event)
        except Exception:  # case intake must never break the decision flow
            logger.exception("[Cases] alert/vaka oluşturulamadı (%s)", event.get("transaction_id"))
            return  # M13: the outbox row stays and is replayed later
        if needs_outbox(event):
            await self.writer.submit(WriteOp("outbox_done", str(event["transaction_id"])))
        if case_id is not None:
            self._spawn(self._enrich_case(case_id, event))

    # --- shared runtime config (H3) -----------------------------------------------------
    async def set_thresholds(
        self, step_up: float, hold: float, block: float, *, actor: str = "system"
    ) -> Thresholds:
        """Publish new policy thresholds to every worker, then apply them here
        (A5: a failed publish leaves this worker unchanged, not diverged)."""
        new = Thresholds(step_up, hold, block)  # validates
        await self.config.publish("thresholds", new.as_dict(), actor=actor)
        return self.engine.policy.set_thresholds(new.step_up, new.hold, new.block)

    async def reload_rules(self, *, publish: bool = True, actor: str = "system") -> str:
        """Rebuild the rule set from the rule table (and tell the other workers)."""
        async with self.db.session() as session:
            ruleset = rule_store.build_ruleset(await rule_store.load_rules(session))
        if publish:  # A5: publish first, so a failure leaves no local divergence
            await self.config.publish("rules", {"version": ruleset.version}, actor=actor)
        self.engine.set_ruleset(ruleset)
        return ruleset.version

    async def _apply_thresholds(self, value: dict[str, Any]) -> None:
        self.engine.policy.set_thresholds(
            float(value["step_up"]), float(value["hold"]), float(value["block"])
        )

    async def _apply_rules(self, value: dict[str, Any]) -> None:
        await self.reload_rules(publish=False)

    async def _apply_models(self, value: dict[str, Any]) -> None:
        await self.reload_models(publish=False)

    async def reload_models(self, *, publish: bool = True) -> dict[str, str | None]:
        """Reload champion/challenger from the registry into the live engine."""
        registry = ModelRegistry(self.settings.resolved_models_dir)
        champion = registry.load_role("champion")
        challenger = registry.load_role("challenger")
        self.engine.set_models(champion, challenger)
        async with self.db.transaction() as session:
            await repo.upsert_models(session, registry.models())
        loaded = {
            "champion": champion.version if champion else None,
            "challenger": challenger.version if challenger else None,
        }
        if publish:
            await self.config.publish("models", dict(loaded))
        return loaded

    async def _approve_promotion(
        self, approval: dict[str, Any], actor: str, tx: ApprovalTx
    ) -> dict[str, Any]:
        """A5: the ``models`` publish commits with the approval; the registry
        file is switched last (restored if the transaction does not commit)
        and the engine reloads only after the commit."""
        version = str(approval["target_id"])
        registry = ModelRegistry(self.settings.resolved_models_dir)
        before = registry.read()
        if version not in before.get("models", {}):
            raise CaseError(f"model bulunamadı: {version}")
        published = await self.config.publish_in(
            tx.session, "models", {"champion": version}, actor=str(approval["requested_by"])
        )
        try:
            registry.promote(version)
        except RegistryError as exc:
            raise CaseError(f"model terfi edilemedi: {exc}") from None
        tx.on_rollback(lambda: registry.restore(before))

        async def _reload() -> None:
            await self.reload_models(publish=False)
            self.config.mark_seen("models", published)
            logger.warning("[Models] %s champion yapıldı (onaylayan %s)", version, actor)

        tx.on_commit(_reload)
        return {"champion": version, "previous_champion": before.get("champion")}

    # --- live dashboard feed ----------------------------------------------------------
    def subscribe_live(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._live.add(queue)
        return queue

    def unsubscribe_live(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._live.discard(queue)

    def _to_live(self, event: dict[str, Any]) -> None:
        item = {
            "transaction_id": event.get("transaction_id"),
            "customer_id": event.get("customer_id"),
            "ts": event.get("ts"),
            "amount_try": event.get("amount_try"),
            "decision": event.get("decision"),
            "risk_score": event.get("risk_score"),
            "latency_ms": event.get("latency_ms"),
            "channel": event.get("channel"),
            "country": event.get("country"),
            "reason": (event.get("risk_explanation") or [None])[0],
            "ring_id": event.get("ring_id"),
        }
        self.recent_live.append({**item, "_t": asyncio.get_running_loop().time()})
        for queue in list(self._live):
            if queue.full():  # slow client: drop the oldest event, never block scoring
                queue.get_nowait()
            queue.put_nowait(item)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def wait_background(self) -> None:
        """Await the write-behind side effects (case intake, live feed, egress)
        and the copilot enrichment tasks they spawn (tests / scenario runner)."""
        while True:
            await self.effects.join()
            if not self._background:
                return
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def settle(self) -> None:
        """Read-your-writes: every decision so far has its case, alerts and
        audit rows committed (enrichment tasks are not awaited)."""
        await self.effects.join()
        await self.writer.flush()

    async def _enrich_case(self, case_id: int, event: dict[str, Any]) -> None:
        """Async, off the scoring path: APP text triage + automatic ŞİB draft.

        The write-behind writer is flushed first so the dossier sees the
        transaction row. A rejected ŞİB draft is retried once, then replaced by
        a deterministic minimal draft; the failure is recorded on the case
        (``COPILOT_ERROR``) and counted — never silently dropped.
        """
        async with self._enrich_slots:  # bounded: shares the CPU with scoring
            await self._enrich_case_now(case_id, event)

    async def _enrich_case_now(self, case_id: int, event: dict[str, Any]) -> None:
        try:
            await self.writer.flush()
            case = await self.cases.get_case(case_id)
            if case["case_type"] == "APP" and event.get("purpose"):
                triage = await self.copilot.triage_text(str(event["purpose"]))
                await self.cases.add_note(
                    case_id,
                    f"[Copilot metin sınıflandırması] {triage.output.label} "
                    f"(güven {triage.output.confidence:.2f}, mod {triage.llm_mode}) — "
                    "skoru etkilemez, yalnızca önceliklendirme bilgisidir.",
                    "copilot",
                )
            if case["case_type"] in self.settings.auto_sib_case_types and not case["sib_draft"]:
                await self._auto_sib(case)
        except Exception:  # enrichment is best-effort, but never silent
            logger.exception("[Copilot] vaka #%s zenginleştirilemedi", case_id)
            metrics.COPILOT_ERRORS.labels(task="enrich").inc()

    async def _auto_sib(self, case: dict[str, Any]) -> None:
        case_id = int(case["id"])
        errors: list[str] = []
        for _ in range(2):  # first attempt + one retry
            try:
                result, _ = await self.copilot.sib_draft(case_id)
            except OutputRejected as exc:
                errors.append(f"{exc.kind}: {exc}")
                continue
            await self.cases.save_sib_draft(
                case_id, result.output.model_dump(mode="json"), "copilot"
            )
            logger.info(
                "[Copilot] vaka #%s için ŞİB taslağı hazırlandı (%s)", case_id, result.llm_mode
            )
            return
        draft = SibDraft.model_validate(minimal_sib_draft(case))
        await self.cases.save_sib_draft(case_id, draft.model_dump(mode="json"), "copilot")
        await self.cases.record_event(
            case_id, "COPILOT_ERROR", "copilot", task="sib_draft", errors=errors[-2:]
        )
        metrics.COPILOT_ERRORS.labels(task="sib_draft").inc()
        logger.error("[Copilot] vaka #%s ŞİB çıktısı reddedildi — minimal taslak", case_id)

    def _profile_summary(self, customer_id: str) -> dict[str, Any] | None:
        store = self.extractor.store
        profiles = getattr(store, "profiles", None)
        profile = profiles.get(customer_id) if profiles is not None else None
        if profile is None:
            return None
        import math

        return {
            "ewma_amount_try": round(math.expm1(profile.log_mean), 2),
            "observations": round(profile.n, 1),
            "max_amount_try": round(profile.max_amount, 2),
            "devices": len(profile.devices),
        }

    def _index_fraud_case(self, case: dict[str, Any]) -> None:
        if self.vector is None:
            return
        codes = sorted(
            {r.get("code", "") for a in case["alerts"] for r in a.get("reason_codes") or []}
        )
        document = (
            f"{case['case_type']} vakası: {case['title']}; {case['alert_count']} alert, toplam "
            f"{case['total_amount_try']:.0f} TL; nedenler: {', '.join(codes)}"
        )
        self.vector.add_case(str(case["id"]), document, {"case_type": case["case_type"]})

    async def _from_ingress(self, payload: dict[str, Any]) -> None:
        """Redis ingress: same strict validation as the HTTP API.

        A payload that fails validation raises, so the Redis bus retries it and
        finally parks it in the dead-letter stream (poison message).
        """
        from app.api.schemas import TransactionIn

        model = TransactionIn.model_validate(payload)
        tx = model.model_dump(mode="json", exclude_none=True)
        tx["ts"] = model.ts.isoformat()
        # durable: another worker's decision counts only once it is committed;
        # IngestInProgress (RetryLater) leaves the message pending for later
        await self.ingest(tx, durable=True)

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
        # A4: the stream channel gets the challenge with the decision
        try:
            slim["step_up_challenge_id"] = await self.issue_step_up(event)
        except Exception:  # publish the decision anyway; a replayed ingest re-issues
            logger.exception("[StepUp] %s için doğrulama üretilemedi", event["transaction_id"])
            slim["step_up_challenge_id"] = None
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
            self.copilot.tools.vector = store
            logger.info("[Vector] RAG deposu hazır (%s gömme)", store.embedding)
        except Exception:
            logger.exception("[Vector] RAG deposu başlatılamadı — copilot RAG'siz çalışacak")

    async def build_drift_reference(self) -> int:
        """Replay the demo population through a throwaway copy of the live engine
        (same rules, model, signals; empty state) and install its feature / score
        distribution as the PSI reference. Returns the number of events used."""
        drift = self.engine.drift
        if drift is None:
            return 0
        from app.scoring.engine import build_signals
        from app.scoring.policy import PolicyEngine

        path = self.settings.resolved_transactions_path
        events = sorted(load_transactions(path), key=lambda t: (t["ts"], t["transaction_id"]))
        extractor = FeatureExtractor(MemoryFeatureStore(), self.customers)
        graph, payees, signals = build_signals(self.customers)
        model = self.engine.model
        shadow = ScoringEngine(
            extractor,
            ruleset=self.engine.ruleset,
            policy=PolicyEngine(model.stacker if model else None),
            sanctions=self.engine.sanctions,
            model=model,
            signals=signals,
            graph=graph,
            payees=payees,
        )
        samples: dict[str, list[float]] = {name: [] for name in drift.reference}
        for tx in events:
            result = await shadow.score(tx)
            if not result.scored:
                continue
            for name, values in samples.items():
                value = result.policy.stacked if name == "score" else result.features.get(name)
                if value is not None:
                    values.append(float(value))
            await shadow.commit(result)
        drift.replace_reference(samples, source="population")
        logger.info("[Drift] PSI referansı demo popülasyonundan kuruldu (%d olay)", len(events))
        return len(events)

    async def _drift_reference_task(self) -> None:
        try:
            await self.build_drift_reference()
        except Exception:  # monitoring must never stop the pipeline
            logger.exception("[Drift] popülasyon referansı kurulamadı — model referansı kullanılır")
            if self.engine.drift is not None:
                self.engine.drift.pending = False

    async def start(self) -> None:
        await self.writer.start()
        await self.effects.start()
        await self.idempotency.load(self.db)
        await self.config.load()  # H3: persisted thresholds survive a restart
        self.config.start(self.settings.config_poll_ms / 1000)
        self.accounts.start_sync(self.settings.account_status_poll_ms / 1000)
        self.outbox.start()
        await self.bus.start()
        if self.ingress is not None:
            await self.ingress.start()
            logger.info("Redis Streams ingress aktif (grup: pipeline)")
        if self.settings.vector_store == "chroma" and self.vector is None:
            self._vector_task = asyncio.create_task(self._init_vector(), name="vector-init")
        drift = self.engine.drift
        if drift is not None and drift.pending:
            self._drift_task = asyncio.create_task(
                self._drift_reference_task(), name="drift-reference"
            )
        flagged = await self.load_graph_flags()
        if flagged:
            logger.info("[Graph] %d doğrulanmış fraud işlemi grafta işaretlendi", flagged)
        if self.graph is not None and self.settings.ring_detect_interval_s > 0:
            self._ring_task = asyncio.create_task(self._ring_loop(), name="ring-detection")
        mode = self.settings.stream_mode
        if mode == "batch" and not await self._warmup_leader():
            mode = "off"  # another worker replays the history into the shared store
        if mode == "batch":
            batch = load_transactions(self.settings.resolved_transactions_path)
            if self.settings.batch_rebase_ts:
                batch = rebase_timestamps(batch)
            self.analyst.observe_drift = False  # warm-up initialises state
            await self.feed(batch)
            await self.drain()
            self.analyst.observe_drift = True
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

    async def _warmup_leader(self) -> bool:
        """With a shared (Redis) feature store only one worker may replay the
        warm-up history, otherwise every ``uvicorn --workers N`` process would
        add the same events to the shared windows/profiles N times."""
        if self.redis is None or self.settings.resolved_feature_store != "redis":
            return True
        key = f"{self.settings.redis_stream_prefix}:warmup:leader"
        if await self.redis.set(key, str(os.getpid()), nx=True, ex=120):
            return True
        logger.info("[Pipeline] ısınma geçmişini başka bir worker yüklüyor — atlandı")
        return False

    async def drain(self) -> None:
        """Wait until the bus is idle, every decision's side effects have run
        and every write is committed."""
        await self.bus.drain()
        await self.effects.join()
        await self.writer.flush()

    async def stop(self) -> None:
        await self.wait_background()
        await self.config.stop()
        await self.accounts.stop_sync()
        await self.outbox.stop()
        for task in (self._stream_task, self._vector_task, self._ring_task, self._drift_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.ingress is not None:
            await self.ingress.stop()
        await self.bus.stop()
        await self.effects.stop()
        await self.wait_background()
        await self.writer.stop()
        await self.idempotency.close()
        await self.extractor.store.close()
        await self.db.dispose()
        if self.redis is not None:
            with contextlib.suppress(Exception):
                await self.redis.aclose()

    # --- ingest ---------------------------------------------------------------------------------
    async def stored_result(self, tx_id: str) -> dict[str, Any] | None:
        """Decision of an already persisted transaction (restart-safe idempotency)."""
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(Transaction, Decision)
                    .join(Decision, Decision.transaction_id == Transaction.id)
                    .where(Transaction.id == tx_id)
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        tx, decision = row
        return {
            "transaction_id": tx.id,
            "customer_id": tx.customer_id,
            "ts": tx.ts,
            "amount": float(tx.amount),
            "currency": tx.currency,
            "amount_try": float(tx.amount_try),
            "device_id": tx.device_id,
            "location": tx.location,
            "channel": tx.channel,
            "country": tx.country,
            "purpose": tx.purpose,
            "beneficiary_id": tx.beneficiary_id,
            "risk_score": decision.risk_score,
            "decision": decision.decision if decision.decision in LEGACY else None,
            "decision_legacy": LEGACY.get(decision.decision, decision.decision),
            "components": decision.components,
            "reason_codes": decision.reason_codes,
            "risk_explanation": [str(r.get("text", "")) for r in decision.reason_codes or []],
            "rule_version": decision.rule_version,
            "model_version": decision.model_version,
            "latency_ms": decision.latency_ms,
            "duplicate": True,
            "persisted_only": True,
            "payload_hash": tx.payload_hash,
        }

    @staticmethod
    def _summary(result: dict[str, Any]) -> dict[str, Any]:
        """Response fields of a decision (the Redis idempotency record)."""
        from app.api.schemas import AnalyzedTransactionOut

        fields = AnalyzedTransactionOut.model_fields
        return {k: v for k, v in result.items() if k in fields or k in ("well_formed", "issues")}

    async def ingest(self, tx: dict[str, Any], *, durable: bool = False) -> dict[str, Any] | None:
        """Score one transaction and return its decision (idempotent per id).

        Request path (V6): in-memory result -> in-flight waiter -> idempotency
        claim (Redis, multi-worker) -> stored decision only when the Bloom
        index says the id may be persisted -> score + decide. Case intake,
        audit, live feed and egress follow write-behind.

        M1: the same id with a different payload raises
        :class:`IdempotencyConflict`; a replay while the id is still being
        scored elsewhere raises :class:`IngestInProgress` after
        ``idempotency_pending_wait_ms`` (both HTTP 409). ``durable=True`` (the
        Redis ingress, which acknowledges only after the DB commit) accepts
        another worker's decision only once it is committed, and always
        checks the database before scoring a redelivered id again.
        """
        tx_id = str(tx.get("transaction_id") or "")
        if not tx_id:
            await self.bus.publish(TransactionMonitor.CREATED, tx)
            if self.bus.running:
                await self.bus.drain()
            return None
        digest = payload_digest(tx)
        tx = {**tx, "payload_hash": digest}
        if tx_id in self.results:
            known = self.results[tx_id]
            check_digest(tx_id, known, digest)
            return {**known, "duplicate": True}
        waiter = self._waiters.get(tx_id)
        if waiter is not None:  # same id already in flight in this process
            if self._inflight.get(tx_id, digest) != digest:
                raise IdempotencyConflict(tx_id)
            try:
                done = await asyncio.wait_for(
                    asyncio.shield(waiter), timeout=self.idempotency.pending_wait_s
                )
            except TimeoutError:
                raise IngestInProgress(tx_id, self.idempotency.retry_after_s) from None
            return {**done, "duplicate": True}
        claimed = await self.idempotency.claim(tx_id, digest)
        if not claimed:  # another worker (or an earlier request) owns this id
            other = await self.idempotency.claimed_result(tx_id, digest, require_committed=durable)
            if other is not None:
                return {**other, "duplicate": True}
            # the owner gave up (released / lease expired): try to take over
            claimed = await self.idempotency.claim(tx_id, digest)
            if not claimed:
                raise IngestInProgress(tx_id, self.idempotency.retry_after_s)
            durable = True  # a lapsed claim may hide a committed decision
        if durable or self.idempotency.maybe_persisted(tx_id):
            try:
                stored = await self.stored_result(tx_id)
                check_digest(tx_id, stored, digest)
            except BaseException:
                await self.idempotency.release(tx_id)
                raise
            if stored is not None:
                await self.idempotency.record(tx_id, self._summary(stored), digest)
                return stored
        self._inflight[tx_id] = digest
        try:
            result = await self._decide(tx_id, tx, durable=durable)
        except IngestInProgress:
            # L1: still being decided; keep the claim until its lease lapses
            self.idempotency.disown(tx_id)
            raise
        except BaseException:
            await self.idempotency.release(tx_id)
            raise
        finally:
            self._inflight.pop(tx_id, None)
        if result is None:
            await self.idempotency.release(tx_id)
        elif self.idempotency.redis is not None:
            # provisional until the writer committed the decision (see _finalize)
            await self.idempotency.record(tx_id, self._summary(result), digest, committed=False)
            self._spawn(self._finalize(tx_id))
        return result

    async def _finalize(self, tx_id: str) -> None:
        """Mark the idempotency record committed once the decision is in the DB."""
        await self.writer.barrier()
        await self.idempotency.commit(tx_id)

    async def _decide(
        self, tx_id: str, tx: dict[str, Any], *, durable: bool = False
    ) -> dict[str, Any] | None:
        if not self.bus.running:  # direct dispatch: handlers run inline
            await self.bus.publish(TransactionMonitor.CREATED, tx, key=tx_id)
            return self.results.get(tx_id)
        # wait for *this* transaction's decision only (no global drain under load)
        waiter = self._waiters.get(tx_id)
        if waiter is None:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters[tx_id] = waiter
            await self.bus.publish(TransactionMonitor.CREATED, tx, key=tx_id)
        return await self._await_decision(tx_id, waiter, durable=durable)

    async def _await_decision(
        self,
        tx_id: str,
        waiter: asyncio.Future[dict[str, Any]],
        *,
        durable: bool = False,
        timeout_s: float = 30.0,
    ) -> dict[str, Any] | None:
        """Wait for the decision of ``tx_id``.

        L1: on the durable (Redis ingress) path a decision that is still
        pending after ``timeout_s`` raises :class:`IngestInProgress` so the
        stream message stays pending instead of being acknowledged unscored.
        """
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout_s)
        except TimeoutError:
            self._waiters.pop(tx_id, None)
            result = self.results.get(tx_id)
            if result is None and durable:
                logger.warning(
                    "[Pipeline] %s kararı %.0fs içinde gelmedi — ack yok", tx_id, timeout_s
                )
                raise IngestInProgress(tx_id, self.idempotency.retry_after_s) from None
            return result


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
        max_retries=settings.writer_max_retries,
        retry_base=settings.writer_retry_base_ms / 1000,
        retry_max=settings.writer_retry_max_ms / 1000,
        transient_timeout=settings.writer_transient_timeout_s,
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
            processing_lease_ms=settings.bus_processing_lease_ms,
            commit_barrier=writer.barrier,  # H4: XACK only after the DB commit
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
