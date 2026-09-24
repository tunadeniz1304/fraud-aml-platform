"""Persistence of the rule DSL (``rules`` + ``rule_versions`` tables).

``rules/core.yaml`` seeds the database on first start; afterwards the database
is the source of truth and every change creates a new immutable
``rule_versions`` row (who, when, full definition) — rule governance.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Decision, Label, Rule, RuleVersion
from app.scoring.rules import RuleDef, RuleError, RuleSet

logger = logging.getLogger("fraud.rules")


def _to_row_values(d: RuleDef, actor: str) -> dict[str, Any]:
    return {
        "name": d.name,
        "description": d.description,
        "when_expr": d.when,
        "score": d.score,
        "reason_code": d.reason_code or d.id,
        "reason_template": d.reason_template,
        "action_hint": d.action_hint,
        "severity": d.severity,
        "enabled": d.enabled,
        "version": d.version,
        "updated_by": actor,
        "updated_at": utcnow(),
    }


def _from_row(row: Rule, tags: Iterable[str] = ()) -> RuleDef:
    return RuleDef(
        id=row.id,
        name=row.name,
        when=row.when_expr,
        score=float(row.score),
        description=row.description,
        reason_code=row.reason_code,
        reason_template=row.reason_template,
        action_hint=row.action_hint,
        severity=row.severity,
        enabled=bool(row.enabled),
        version=int(row.version),
        tags=tuple(tags),
    )


async def seed_rules(session: AsyncSession, definitions: Iterable[RuleDef]) -> int:
    """Insert YAML rules that are not in the database yet (never overwrites)."""
    existing = set((await session.execute(select(Rule.id))).scalars())
    added = 0
    for d in definitions:
        if d.id in existing:
            continue
        d.validate()
        session.add(Rule(id=d.id, **_to_row_values(d, "seed")))
        session.add(
            RuleVersion(rule_id=d.id, version=d.version, definition=d.to_dict(), created_by="seed")
        )
        added += 1
    return added


async def _latest_tags(session: AsyncSession) -> dict[str, list[str]]:
    latest = (
        select(RuleVersion.rule_id, func.max(RuleVersion.version).label("v"))
        .group_by(RuleVersion.rule_id)
        .subquery()
    )
    rows = await session.execute(
        select(RuleVersion.rule_id, RuleVersion.definition).join(
            latest,
            (RuleVersion.rule_id == latest.c.rule_id) & (RuleVersion.version == latest.c.v),
        )
    )
    return {rid: list((doc or {}).get("tags") or []) for rid, doc in rows.all()}


async def load_rules(session: AsyncSession) -> list[RuleDef]:
    tags = await _latest_tags(session)
    rows = (await session.execute(select(Rule).order_by(Rule.id))).scalars()
    return [_from_row(r, tags.get(r.id, ())) for r in rows]


async def get_rule(session: AsyncSession, rule_id: str) -> RuleDef | None:
    row = await session.get(Rule, rule_id)
    if row is None:
        return None
    return _from_row(row, (await _latest_tags(session)).get(rule_id, ()))


async def rule_history(session: AsyncSession, rule_id: str) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(RuleVersion)
        .where(RuleVersion.rule_id == rule_id)
        .order_by(RuleVersion.version.desc())
    )
    return [
        {
            "version": r.version,
            "definition": r.definition,
            "created_by": r.created_by,
            "created_at": r.created_at,
        }
        for r in rows.scalars()
    ]


async def save_rule(session: AsyncSession, definition: RuleDef, actor: str) -> RuleDef:
    """Create (v1) or update (version + 1) a rule and append its version row."""
    definition.validate()
    row = await session.get(Rule, definition.id)
    version = 1 if row is None else int(row.version) + 1
    stored = RuleDef.from_dict({**definition.to_dict(), "version": version})
    values = _to_row_values(stored, actor)
    if row is None:
        session.add(Rule(id=stored.id, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
    session.add(
        RuleVersion(
            rule_id=stored.id, version=version, definition=stored.to_dict(), created_by=actor
        )
    )
    return stored


def build_ruleset(definitions: Iterable[RuleDef]) -> RuleSet:
    """Compile rules, skipping (and logging) definitions that no longer compile."""
    valid: list[RuleDef] = []
    for d in definitions:
        try:
            d.validate()
            valid.append(d)
        except RuleError as exc:
            logger.error("[Rules] kural devre dışı bırakıldı (derlenemedi): %s", exc)
    return RuleSet.from_definitions(valid)


# --- backtest data -------------------------------------------------------------------
async def db_backtest_rows(
    session: AsyncSession, limit: int
) -> list[tuple[dict[str, float], int | None]]:
    rows = await session.execute(
        select(Decision.features, Label.label)
        .outerjoin(Label, Label.transaction_id == Decision.transaction_id)
        .order_by(Decision.id.desc())
        .limit(limit)
    )
    # unscored decisions (blocked account, unknown customer) carry no features
    return [(dict(features), label) for features, label in rows.all() if features]


def synthetic_backtest_rows(path: Path, limit: int) -> list[tuple[dict[str, float], int | None]]:
    out: list[tuple[dict[str, float], int | None]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if len(out) >= limit:
                break
            doc = json.loads(line)
            out.append((doc["f"], int(doc["y"])))
    return out
