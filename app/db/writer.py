"""Write-behind persistence writer.

The synchronous scoring path never waits for the database: decisions,
transactions, account status changes and audit entries are queued and flushed
in batches (``batch_size`` items or ``flush_interval`` seconds, whichever comes
first) inside a single DB transaction. The queue is bounded, so a slow database
applies **backpressure** to producers instead of growing memory without limit.

A single consumer also serialises audit-chain appends in submission order.
When the writer is not started (scripts, some tests) every submit is applied
immediately in its own transaction.

Failure handling (H4) — a batch is **never dropped silently**:

* transient errors (connection lost, ``database is locked``, deadlock /
  serialisation failures) are retried with capped exponential backoff for as
  long as they last; the bounded queue turns the outage into backpressure;
* any other error is retried ``max_retries`` times, then the batch is
  **bisected** until the poison operation(s) are isolated. Poison operations
  go to the ``dead_letters`` table, every good operation is committed;
* if even the dead-letter insert fails the operation is logged at CRITICAL
  with its full payload and counted, so it can be replayed by hand.

:meth:`barrier` resolves once every operation submitted before it has been
committed (or dead-lettered); the ingress consumer acknowledges its messages
only after that point (at-least-once delivery + idempotent inserts).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.exc import DBAPIError

from app.db import repository as repo
from app.db.audit import AuditChain, AuditEntry
from app.db.database import Database
from app.db.models import DeadLetterRecord
from app.monitoring import metrics

logger = logging.getLogger("fraud.db.writer")

OpKind = Literal["transaction", "decision", "status", "audit", "outbox", "outbox_done"]

_TRANSIENT_MARKERS = (
    "database is locked",
    "database table is locked",
    "deadlock",
    "could not serialize",
    "serialization failure",
    "connection",
    "server closed",
    "timeout",
    "timed out",
    "too many clients",
)


@dataclass
class StatusChange:
    customer_id: str
    previous: str
    new: str
    reason: str
    actor: str = "system"
    transaction_id: str | None = None
    #: status generation (``accounts.version`` after the change) when known
    version: int | None = None


@dataclass
class WriteOp:
    kind: OpKind
    data: Any


@dataclass
class _Barrier:
    future: asyncio.Future[None]


@dataclass
class _Batch:
    transactions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[StatusChange] = field(default_factory=list)
    audits: list[AuditEntry] = field(default_factory=list)
    outbox: list[dict[str, Any]] = field(default_factory=list)
    outbox_done: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, ops: list[WriteOp]) -> _Batch:
        batch = cls()
        for op in ops:
            batch.add(op)
        return batch

    def add(self, op: WriteOp) -> None:
        if op.kind == "transaction":
            self.transactions.append(op.data)
        elif op.kind == "decision":
            self.decisions.append(op.data)
        elif op.kind == "status":
            self.statuses.append(op.data)
        elif op.kind == "outbox":
            self.outbox.append(op.data)
        elif op.kind == "outbox_done":
            self.outbox_done.append(str(op.data))
        else:
            self.audits.append(op.data)

    def __len__(self) -> int:
        return (
            len(self.transactions)
            + len(self.decisions)
            + len(self.statuses)
            + len(self.audits)
            + len(self.outbox)
            + len(self.outbox_done)
        )


def is_transient(exc: BaseException) -> bool:
    """Errors worth waiting out (the database is unavailable or contended)."""
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    if isinstance(exc, DBAPIError | OSError):
        text = str(exc).lower()
        return any(marker in text for marker in _TRANSIENT_MARKERS)
    return False


def _op_key(op: WriteOp) -> str:
    data = op.data
    if isinstance(data, dict):
        return str(data.get("transaction_id") or data.get("id") or data.get("customer_id") or "")
    if isinstance(data, StatusChange):
        return data.customer_id
    if isinstance(data, AuditEntry):
        return str(data.transaction_id or data.entity_id or data.customer_id or "")
    return str(data)


def _op_payload(op: WriteOp) -> dict[str, Any]:
    data = op.data
    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        data = dataclasses.asdict(data)
    elif not isinstance(data, dict):
        data = {"value": data}
    # JSON-safe copy (datetimes, Decimals ...) of exactly what was submitted
    doc: dict[str, Any] = json.loads(json.dumps(data, default=str, ensure_ascii=False))
    return doc


class PersistenceWriter:
    def __init__(
        self,
        db: Database,
        chain: AuditChain | None = None,
        *,
        batch_size: int = 500,
        flush_interval: float = 0.05,
        queue_size: int = 20_000,
        max_retries: int = 2,
        retry_base: float = 0.05,
        retry_max: float = 2.0,
    ) -> None:
        self.db = db
        self.chain = chain or AuditChain()
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_retries = max_retries
        self.retry_base = retry_base
        self.retry_max = retry_max
        self._queue: asyncio.Queue[WriteOp | _Barrier] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self.batches_written = 0
        self.errors = 0
        self.retries = 0
        self.dead_lettered = 0
        self.lost = 0  # dead-letter insert failed too (logged CRITICAL with payload)

    # --- lifecycle ---------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="db-writer")

    async def stop(self) -> None:
        if self.running:
            await self.flush()
            assert self._task is not None
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def flush(self) -> None:
        """Wait until everything submitted so far is committed."""
        if self.running:
            await self._queue.join()

    async def barrier(self) -> None:
        """Wait until every operation submitted before this call is committed
        (or dead-lettered). Cheaper than :meth:`flush` under load: it does not
        wait for operations submitted later."""
        if not self.running:
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(_Barrier(future))
        await asyncio.shield(future)

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    # --- producers -----------------------------------------------------------------
    async def submit(self, *ops: WriteOp) -> None:
        if not self.running:
            await self._commit(list(ops))
            return
        for op in ops:
            await self._queue.put(op)  # blocks when full -> backpressure

    async def record_decision(
        self,
        transaction: dict[str, Any],
        decision: dict[str, Any],
        audit: AuditEntry,
        *,
        outbox: dict[str, Any] | None = None,
    ) -> None:
        ops = [
            WriteOp("transaction", transaction),
            WriteOp("decision", decision),
            WriteOp("audit", audit),
        ]
        if outbox is not None:
            ops.append(WriteOp("outbox", outbox))
        await self.submit(*ops)

    async def record_status(self, change: StatusChange, audit: AuditEntry) -> None:
        await self.submit(WriteOp("status", change), WriteOp("audit", audit))

    async def record_audit(self, audit: AuditEntry) -> None:
        await self.submit(WriteOp("audit", audit))

    # --- consumer --------------------------------------------------------------------
    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            items = [await self._queue.get()]
            deadline = loop.time() + self.flush_interval
            while len(items) < self.batch_size:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    items.append(await asyncio.wait_for(self._queue.get(), timeout))
                except TimeoutError:
                    break
            ops = [item for item in items if isinstance(item, WriteOp)]
            try:
                if ops:
                    await self._commit(ops)
            finally:
                for item in items:
                    if isinstance(item, _Barrier) and not item.future.done():
                        item.future.set_result(None)
                    self._queue.task_done()

    async def _commit(self, ops: list[WriteOp], *, retries: int | None = None) -> None:
        """Commit ``ops`` (retry, then bisect; poison ops go to the DLQ)."""
        budget = self.max_retries if retries is None else retries
        delay = self.retry_base
        failures = 0
        while True:
            try:
                await self._apply(_Batch.of(ops))
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified, retried or dead-lettered
                self.errors += 1
                metrics.DB_WRITE_ERRORS.inc()
                if is_transient(exc):
                    self.retries += 1
                    logger.warning(
                        "[DB] %d kayıtlık toplu yazım geçici hata (%s) — %.2fs sonra tekrar",
                        len(ops),
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.retry_max)
                    continue
                failures += 1
                if failures <= budget:
                    self.retries += 1
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.retry_max)
                    continue
                error = exc
                break
        if len(ops) == 1:
            await self._dead_letter(ops[0], error, attempts=failures)
            return
        logger.warning("[DB] %d kayıtlık toplu yazım bölünüyor (%s)", len(ops), error)
        middle = len(ops) // 2
        await self._commit(ops[:middle], retries=0)
        await self._commit(ops[middle:], retries=0)

    async def _dead_letter(self, op: WriteOp, error: BaseException, *, attempts: int) -> None:
        payload = _op_payload(op)
        record = {
            "source": "writer",
            "kind": op.kind,
            "key": _op_key(op)[:128],
            "error": f"{type(error).__name__}: {error}"[:2000],
            "attempts": attempts,
            "payload": payload,
        }
        delay = self.retry_base
        for _ in range(max(1, self.max_retries + 1)):
            try:
                async with self.db.transaction() as session:
                    session.add(DeadLetterRecord(**record))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - fall back to the CRITICAL log below
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_max)
                continue
            self.dead_lettered += 1
            metrics.WRITER_DEAD_LETTERS.labels(outcome="stored").inc()
            logger.error(
                "[DB] kalıcılaştırılamayan %s kaydı DLQ'ya alındı (%s): %s",
                op.kind,
                record["key"],
                record["error"],
            )
            return
        self.lost += 1
        metrics.WRITER_DEAD_LETTERS.labels(outcome="log_only").inc()
        logger.critical(
            "[DB] %s kaydı ne yazılabildi ne DLQ'ya alınabildi — elle yeniden oynatın: %s",
            op.kind,
            json.dumps(record, ensure_ascii=False, default=str),
        )

    async def _apply(self, batch: _Batch) -> None:
        async with self.chain.lock, self.db.transaction() as session:
            await repo.insert_transactions(session, batch.transactions)
            await repo.insert_decisions(session, batch.decisions)
            for change in batch.statuses:
                await repo.set_account_status(
                    session,
                    change.customer_id,
                    change.new,
                    previous=change.previous,
                    reason=change.reason,
                    actor=change.actor,
                    transaction_id=change.transaction_id,
                )
            await repo.insert_case_outbox(session, batch.outbox)
            await self.chain.append_many(session, batch.audits)
            await repo.delete_case_outbox(session, batch.outbox_done)
        self.batches_written += 1
        metrics.DB_BATCH_SIZE.observe(len(batch))
