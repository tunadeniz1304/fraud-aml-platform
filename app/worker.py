"""Background worker (compose ``worker`` service).

Consumes the ``decision.made`` egress stream with its own consumer group and
runs periodic batch jobs over the database. Jobs are registered in
:data:`JOBS`; later phases add ring detection, drift snapshots and
label-driven retraining checks. The worker never touches the synchronous
scoring path.

    python -m app.worker
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.db.audit import verify_chain
from app.db.database import Database
from app.monitoring.logging import configure_logging

logger = logging.getLogger("fraud.worker")


@dataclass
class WorkerContext:
    db: Database
    redis: Any
    decisions_seen: Counter[str] = field(default_factory=Counter)
    last_results: dict[str, Any] = field(default_factory=dict)


JobFn = Callable[[WorkerContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Job:
    name: str
    interval_s: float
    fn: JobFn


JOBS: list[Job] = []


def job(name: str, interval_s: float) -> Callable[[JobFn], JobFn]:
    def deco(fn: JobFn) -> JobFn:
        JOBS.append(Job(name, interval_s, fn))
        return fn

    return deco


@job("audit_verify", interval_s=600)
async def audit_verify(ctx: WorkerContext) -> dict[str, Any]:
    async with ctx.db.session() as session:
        result = await verify_chain(session)
    level = logging.INFO if result.ok else logging.CRITICAL
    logger.log(level, "[Worker] audit zinciri: %s (%d satır)", result.detail, result.checked)
    return result.as_dict()


@job("case_sla", interval_s=60)
async def case_sla(ctx: WorkerContext) -> dict[str, Any]:
    """Internal 4 h SLA and MASAK 10-business-day deadline monitoring."""
    from app.cases.service import CaseService

    result = await CaseService(ctx.db).sla_scan()
    if result["internal_sla_breached"] or result["masak_due_soon"] or result["masak_overdue"]:
        logger.warning(
            "[Worker] SLA: %d vaka iç SLA'yı aştı, %d vakanın MASAK süresi dolmak üzere, "
            "%d vakanın MASAK süresi geçti",
            result["internal_sla_breached"],
            result["masak_due_soon"],
            result["masak_overdue"],
        )
    return result


@job("ring_detection", interval_s=300)
async def ring_detection(ctx: WorkerContext) -> dict[str, Any]:
    """Louvain mule-ring detection over the last 7 days of transactions."""
    from app.graph.batch import detect_and_store

    result = await detect_and_store(ctx.db)
    if result["rings"]:
        logger.warning("[Worker] %d şüpheli halka: %s", result["rings"], result["top"])
    return result


@job("retrain_check", interval_s=900)
async def retrain_check(ctx: WorkerContext) -> dict[str, Any]:
    """Feedback loop: recommend retraining once enough new analyst labels exist."""
    from sqlalchemy import func, select

    from app.db.models import Label, ModelVersion

    settings = get_settings()
    async with ctx.db.session() as session:
        since = (
            await session.execute(
                select(ModelVersion.created_at).where(ModelVersion.status == "champion")
            )
        ).scalar_one_or_none()
        query = select(func.count()).select_from(Label).where(Label.source == "analyst")
        if since is not None:
            query = query.where(Label.created_at >= since)
        new_labels = int((await session.execute(query)).scalar_one())
    due = new_labels >= settings.retrain_min_new_labels
    if due:
        logger.warning(
            "[Worker] %d yeni analist etiketi: 'python scripts/train_models.py --incremental "
            "--status challenger --version fraud_gbm_vN' önerilir",
            new_labels,
        )
    return {"new_labels": new_labels, "retrain_recommended": due}


@job("decision_stats", interval_s=60)
async def decision_stats(ctx: WorkerContext) -> dict[str, Any]:
    stats = dict(ctx.decisions_seen)
    logger.info("[Worker] karar dağılımı (egress): %s", stats)
    return stats


async def _run_job(ctx: WorkerContext, item: Job, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            ctx.last_results[item.name] = await item.fn(ctx)
        except Exception:  # one failing job must not stop the worker
            logger.exception("[Worker] iş '%s' başarısız", item.name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=item.interval_s)


async def run_worker(stop: asyncio.Event | None = None) -> WorkerContext:
    import redis.asyncio as aioredis

    from app.bus.redis_streams import RedisStreamsBus

    settings = get_settings()
    stop = stop or asyncio.Event()
    db = Database(settings.resolved_database_url)
    redis = (
        aioredis.from_url(settings.redis_url, decode_responses=True) if settings.redis_url else None
    )
    ctx = WorkerContext(db=db, redis=redis)
    bus = None
    if redis is not None:
        bus = RedisStreamsBus(redis, prefix=settings.redis_stream_prefix, consumer="worker")

        async def on_decision(event: dict[str, Any]) -> None:
            ctx.decisions_seen[str(event.get("decision") or event.get("decision_legacy"))] += 1

        bus.subscribe("decision.made", on_decision, group="worker")
        await bus.start()
    logger.info("[Worker] başladı: %d iş (%s)", len(JOBS), ", ".join(j.name for j in JOBS))
    tasks = [asyncio.create_task(_run_job(ctx, j, stop), name=f"job-{j.name}") for j in JOBS]
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if bus is not None:
        await bus.stop()
    if redis is not None:
        await redis.aclose()
    await db.dispose()
    return ctx


def main() -> None:
    configure_logging(get_settings())
    stop = asyncio.Event()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, stop.set)
        await run_worker(stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
