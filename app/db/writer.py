"""Write-behind persistence writer.

The synchronous scoring path never waits for the database: decisions,
transactions, account status changes and audit entries are queued and flushed
in batches (``batch_size`` items or ``flush_interval`` seconds, whichever comes
first) inside a single DB transaction. The queue is bounded, so a slow database
applies **backpressure** to producers instead of growing memory without limit.

A single consumer also serialises audit-chain appends in submission order.
When the writer is not started (scripts, some tests) every submit is applied
immediately in its own transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from app.db import repository as repo
from app.db.audit import AuditChain, AuditEntry
from app.db.database import Database
from app.monitoring import metrics

logger = logging.getLogger("fraud.db.writer")


@dataclass
class StatusChange:
    customer_id: str
    previous: str
    new: str
    reason: str
    actor: str = "system"
    transaction_id: str | None = None


@dataclass
class WriteOp:
    kind: Literal["transaction", "decision", "status", "audit"]
    data: Any


@dataclass
class _Batch:
    transactions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[StatusChange] = field(default_factory=list)
    audits: list[AuditEntry] = field(default_factory=list)

    def add(self, op: WriteOp) -> None:
        if op.kind == "transaction":
            self.transactions.append(op.data)
        elif op.kind == "decision":
            self.decisions.append(op.data)
        elif op.kind == "status":
            self.statuses.append(op.data)
        else:
            self.audits.append(op.data)

    def __len__(self) -> int:
        return len(self.transactions) + len(self.decisions) + len(self.statuses) + len(self.audits)


class PersistenceWriter:
    def __init__(
        self,
        db: Database,
        chain: AuditChain | None = None,
        *,
        batch_size: int = 500,
        flush_interval: float = 0.05,
        queue_size: int = 20_000,
    ) -> None:
        self.db = db
        self.chain = chain or AuditChain()
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._queue: asyncio.Queue[WriteOp] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self.batches_written = 0
        self.errors = 0

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

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    # --- producers -----------------------------------------------------------------
    async def submit(self, *ops: WriteOp) -> None:
        if not self.running:
            batch = _Batch()
            for op in ops:
                batch.add(op)
            await self._apply(batch)
            return
        for op in ops:
            await self._queue.put(op)  # blocks when full -> backpressure

    async def record_decision(
        self,
        transaction: dict[str, Any],
        decision: dict[str, Any],
        audit: AuditEntry,
    ) -> None:
        await self.submit(
            WriteOp("transaction", transaction),
            WriteOp("decision", decision),
            WriteOp("audit", audit),
        )

    async def record_status(self, change: StatusChange, audit: AuditEntry) -> None:
        await self.submit(WriteOp("status", change), WriteOp("audit", audit))

    async def record_audit(self, audit: AuditEntry) -> None:
        await self.submit(WriteOp("audit", audit))

    # --- consumer --------------------------------------------------------------------
    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            first = await self._queue.get()
            batch = _Batch()
            batch.add(first)
            taken = 1
            deadline = loop.time() + self.flush_interval
            while taken < self.batch_size:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    batch.add(await asyncio.wait_for(self._queue.get(), timeout))
                    taken += 1
                except TimeoutError:
                    break
            try:
                await self._apply(batch)
            except Exception:
                self.errors += 1
                metrics.DB_WRITE_ERRORS.inc()
                logger.exception("[DB] %d kayıtlık toplu yazım başarısız", len(batch))
            finally:
                for _ in range(taken):
                    self._queue.task_done()

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
            await self.chain.append_many(session, batch.audits)
        self.batches_written += 1
        metrics.DB_BATCH_SIZE.observe(len(batch))
