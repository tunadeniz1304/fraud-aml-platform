"""Versioned runtime configuration shared by every worker (H3).

``uvicorn --workers N`` runs N pipelines, each with its own in-memory scoring
engine. A policy-threshold change, a rule edit or a champion-model swap made
through one worker's API used to reach that worker only (and thresholds were
lost on restart). Every such change is now published to the ``runtime_config``
table as ``(key, value, version)``:

* :meth:`ConfigSync.publish` bumps the key's version with a compare-and-set
  (``UPDATE ... WHERE version=:v``; a concurrent publisher retries on top);
* :meth:`ConfigSync.load` applies every stored key at start-up, so persisted
  thresholds survive a restart;
* :meth:`ConfigSync.poll` (every ``CONFIG_POLL_MS``, default 1 s) applies any key
  whose version moved past the one this worker last applied.

Keys: ``thresholds`` (the value *is* the config), ``rules`` and ``models``
(generation counters — the payload is informative, the handler reloads from
the rule table / model registry).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.database import Database
from app.db.models import RuntimeConfig

logger = logging.getLogger("fraud.config_sync")

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class ConfigConflict(RuntimeError):
    """The key kept changing under the compare-and-set."""


class ConfigSync:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.handlers: dict[str, Handler] = {}
        self.restore: set[str] = set()  # keys re-applied from the table at start-up
        self.seen: dict[str, int] = {}  # version applied (or published) by this worker
        self.applied = 0
        self._task: asyncio.Task[None] | None = None

    def register(self, key: str, handler: Handler, *, restore: bool = False) -> None:
        """``restore``: apply the stored value at start-up (thresholds). Keys whose
        source of truth is loaded anyway (rules table, model registry) are only
        marked as seen at start-up."""
        self.handlers[key] = handler
        if restore:
            self.restore.add(key)

    async def read(self) -> dict[str, tuple[dict[str, Any], int]]:
        async with self.db.session() as session:
            rows = await session.execute(
                select(RuntimeConfig.key, RuntimeConfig.value, RuntimeConfig.version)
            )
            return {key: (dict(value or {}), int(version)) for key, value, version in rows.all()}

    async def publish(
        self, key: str, value: dict[str, Any], *, actor: str = "system", attempts: int = 5
    ) -> int:
        """Store ``value`` under the next version of ``key``; returns that version."""
        for _ in range(max(1, attempts)):
            try:
                async with self.db.transaction() as session:
                    version = await self.publish_in(session, key, value, actor=actor)
            except ConfigConflict:
                continue  # another worker published in between
            except IntegrityError:  # two first publishes raced on the insert
                continue
            self.mark_seen(key, version)
            logger.info("[Config] %s v%d yayınlandı (%s)", key, version, actor)
            return version
        raise ConfigConflict(key)

    async def publish_in(
        self, session: AsyncSession, key: str, value: dict[str, Any], *, actor: str = "system"
    ) -> int:
        """Compare-and-set ``key`` to its next version inside the caller's
        transaction (A5: a maker-checker approval publishes atomically with its
        own writes). Raises :class:`ConfigConflict` if a concurrent publisher
        won; the caller calls :meth:`mark_seen` after the commit."""
        current = (
            await session.execute(select(RuntimeConfig.version).where(RuntimeConfig.key == key))
        ).scalar_one_or_none()
        if current is None:
            session.add(
                RuntimeConfig(
                    key=key, value=value, version=1, updated_by=actor, updated_at=utcnow()
                )
            )
            await session.flush()
            return 1
        result = await session.execute(
            update(RuntimeConfig)
            .where(RuntimeConfig.key == key, RuntimeConfig.version == current)
            .values(value=value, version=current + 1, updated_by=actor, updated_at=utcnow())
        )
        if (result.rowcount or 0) != 1:  # type: ignore[attr-defined]
            raise ConfigConflict(key)
        return int(current) + 1

    def mark_seen(self, key: str, version: int) -> None:
        """This worker applied (or published) ``version`` of ``key``."""
        self.seen[key] = max(self.seen.get(key, 0), version)

    async def load(self) -> int:
        """Start-up: restore persisted keys (thresholds), mark the rest as seen."""
        return await self._apply_newer(force=True)

    async def poll(self) -> int:
        """Apply keys whose version moved since this worker last saw them."""
        return await self._apply_newer(force=False)

    async def _apply_newer(self, *, force: bool) -> int:
        applied = 0
        for key, (value, version) in (await self.read()).items():
            if force and key not in self.restore:
                self.seen[key] = max(self.seen.get(key, 0), version)
                continue
            if not force and version <= self.seen.get(key, 0):
                continue
            handler = self.handlers.get(key)
            if handler is None:
                continue
            try:
                await handler(value)
            except Exception:  # a bad value must not stop the others / the poller
                logger.exception("[Config] %s v%d uygulanamadı", key, version)
                continue
            self.seen[key] = version
            applied += 1
            logger.info("[Config] %s v%d uygulandı", key, version)
        self.applied += applied
        return applied

    def start(self, interval_s: float) -> None:
        if interval_s > 0 and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop(interval_s), name="config-sync")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.poll()
            except Exception:
                logger.exception("[Config] senkronizasyon başarısız")
