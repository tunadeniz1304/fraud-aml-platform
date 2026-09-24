"""Rule engine (P0.5): YAML/DB rule DSL, noisy-OR rule score and backtesting.

A rule is data, not code: its ``when`` clause is compiled by the safe
expression compiler (:mod:`app.scoring.expressions`) against the feature
registry, so an unknown field fails when the rule is *loaded*. Rules carry a
score (0..1 strength), a Turkish reason template and an optional
``action_hint`` that acts as a policy floor.

Rule score of a transaction = noisy-OR of the fired rules' scores,
``1 - Π(1 - s_i)`` — several weak signals add up, one strong signal is enough,
and the result stays in ``[0, 1]``. The ruleset version is a hash of the
enabled rule definitions, stored on every decision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from app.config import BASE_DIR
from app.features.definitions import feature_names
from app.scoring.expressions import CompiledExpression, ExpressionError, compile_expression
from app.scoring.reasons import render_template

ACTIONS = ("ALLOW", "STEP_UP", "HOLD", "BLOCK")
SEVERITIES = ("low", "medium", "high", "critical")
DEFAULT_RULES_PATH = BASE_DIR / "rules" / "core.yaml"


class RuleError(ValueError):
    """Invalid rule definition (message is user-facing Turkish)."""


@dataclass(frozen=True)
class RuleDef:
    """Serializable rule definition (YAML / DB / API)."""

    id: str
    name: str
    when: str
    score: float
    description: str = ""
    reason_code: str = ""
    reason_template: str = ""
    action_hint: str | None = None
    severity: str = "medium"
    enabled: bool = True
    version: int = 1
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RuleDef:
        try:
            rule_id = str(data["id"]).strip()
            when = str(data["when"]).strip()
            score = float(data["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuleError(f"kural tanımı eksik/geçersiz: {exc}") from None
        hint = data.get("action_hint")
        return cls(
            id=rule_id,
            name=str(data.get("name") or rule_id),
            when=when,
            score=score,
            description=str(data.get("description") or ""),
            reason_code=str(data.get("reason_code") or rule_id),
            reason_template=str(data.get("reason_template") or data.get("name") or rule_id),
            action_hint=str(hint).upper() if hint else None,
            severity=str(data.get("severity") or "medium").lower(),
            enabled=bool(data.get("enabled", True)),
            version=int(data.get("version") or 1),
            tags=tuple(str(t) for t in data.get("tags") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["tags"] = list(self.tags)
        return out

    def validate(self, names: Iterable[str] | None = None) -> CompiledExpression:
        if not self.id or not self.id.replace("_", "").isalnum():
            raise RuleError("kural kimliği yalnızca harf, rakam ve '_' içerebilir")
        if not 0.0 < self.score <= 1.0:
            raise RuleError(f"{self.id}: skor (0, 1] aralığında olmalı")
        if self.action_hint is not None and self.action_hint not in ACTIONS:
            raise RuleError(
                f"{self.id}: action_hint {', '.join(ACTIONS)} değerlerinden biri olmalı"
            )
        if self.severity not in SEVERITIES:
            raise RuleError(f"{self.id}: severity {', '.join(SEVERITIES)} olmalı")
        try:
            return compile_expression(self.when, names if names is not None else feature_names())
        except ExpressionError as exc:
            raise RuleError(f"{self.id}: {exc}") from None


@dataclass(frozen=True)
class CompiledRule:
    definition: RuleDef
    expression: CompiledExpression

    @property
    def id(self) -> str:
        return self.definition.id


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    reason_code: str
    score: float
    text: str
    action_hint: str | None
    severity: str
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "code": self.reason_code,
            "score": self.score,
            "text": self.text,
            "action_hint": self.action_hint,
            "severity": self.severity,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class RuleEvaluation:
    hits: tuple[RuleHit, ...]
    score: float
    action_hint: str | None
    version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "action_hint": self.action_hint,
            "version": self.version,
            "hits": [h.as_dict() for h in self.hits],
        }


def noisy_or(scores: Iterable[float]) -> float:
    remaining = 1.0
    for s in scores:
        remaining *= 1.0 - min(1.0, max(0.0, s))
    return 1.0 - remaining


def strongest_action(actions: Iterable[str | None]) -> str | None:
    best: str | None = None
    for action in actions:
        if action and (best is None or ACTIONS.index(action) > ACTIONS.index(best)):
            best = action
    return best


def ruleset_version(definitions: Iterable[RuleDef]) -> str:
    canonical = sorted(
        (d.id, d.when, round(d.score, 6), d.action_hint or "", d.version)
        for d in definitions
        if d.enabled
    )
    digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode("utf-8"))
    return "rs-" + digest.hexdigest()[:12]


@dataclass
class RuleSet:
    """Compiled, immutable-by-convention set of rules (swap atomically to reload)."""

    rules: list[CompiledRule] = field(default_factory=list)
    version: str = "rs-empty"

    @classmethod
    def from_definitions(
        cls, definitions: Iterable[RuleDef], *, names: Sequence[str] | None = None
    ) -> RuleSet:
        allowed = list(names) if names is not None else feature_names()
        compiled: list[CompiledRule] = []
        seen: set[str] = set()
        defs = list(definitions)
        for d in defs:
            if d.id in seen:
                raise RuleError(f"kural kimliği tekrar ediyor: {d.id}")
            seen.add(d.id)
            expression = d.validate(allowed)
            if d.enabled:
                compiled.append(CompiledRule(d, expression))
        return cls(compiled, ruleset_version(defs))

    @property
    def definitions(self) -> list[RuleDef]:
        return [r.definition for r in self.rules]

    def evaluate(self, features: Mapping[str, float]) -> RuleEvaluation:
        hits: list[RuleHit] = []
        for rule in self.rules:
            if rule.expression.evaluate_bool(features):
                d = rule.definition
                hits.append(
                    RuleHit(
                        rule_id=d.id,
                        reason_code=d.reason_code or d.id,
                        score=d.score,
                        text=render_template(d.reason_template, features),
                        action_hint=d.action_hint,
                        severity=d.severity,
                        tags=d.tags,
                    )
                )
        hits.sort(key=lambda h: -h.score)
        return RuleEvaluation(
            hits=tuple(hits),
            score=round(noisy_or(h.score for h in hits), 6),
            action_hint=strongest_action(h.action_hint for h in hits),
            version=self.version,
        )

    def score_only(self, features: Mapping[str, float]) -> float:
        """Rule score without building hit objects (training backfill)."""
        remaining = 1.0
        for rule in self.rules:
            if rule.expression.evaluate_bool(features):
                remaining *= 1.0 - rule.definition.score
        return 1.0 - remaining


def load_rule_file(path: Path | None = None) -> list[RuleDef]:
    path = path or DEFAULT_RULES_PATH
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return [RuleDef.from_dict(r) for r in data.get("rules") or []]


def load_ruleset(path: Path | None = None) -> RuleSet:
    return RuleSet.from_definitions(load_rule_file(path))


# --- backtesting ---------------------------------------------------------------------
@dataclass(frozen=True)
class BacktestResult:
    rule_id: str
    evaluated: int
    alerts: int
    labelled_alerts: int
    true_positives: int
    positives: int
    source: str

    @property
    def alert_rate(self) -> float:
        return self.alerts / self.evaluated if self.evaluated else 0.0

    @property
    def precision(self) -> float | None:
        return self.true_positives / self.labelled_alerts if self.labelled_alerts else None

    @property
    def recall(self) -> float | None:
        return self.true_positives / self.positives if self.positives else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "source": self.source,
            "evaluated": self.evaluated,
            "alerts": self.alerts,
            "alert_rate": round(self.alert_rate, 6),
            "labelled_alerts": self.labelled_alerts,
            "true_positives": self.true_positives,
            "positives": self.positives,
            "precision": None if self.precision is None else round(self.precision, 4),
            "recall": None if self.recall is None else round(self.recall, 4),
        }


def backtest(
    definition: RuleDef,
    rows: Iterable[tuple[Mapping[str, float], int | None]],
    *,
    source: str = "db",
) -> BacktestResult:
    """Replay a rule over historical ``(features, label)`` rows.

    ``label`` is ``1`` (fraud), ``0`` (clean) or ``None`` (unlabelled — counts
    towards the alert volume but not towards precision/recall).
    """
    expression = definition.validate()
    evaluated = alerts = labelled = tp = positives = 0
    for features, label in rows:
        evaluated += 1
        fired = expression.evaluate_bool(features)
        if label == 1:
            positives += 1
        if fired:
            alerts += 1
            if label is not None:
                labelled += 1
                tp += int(label == 1)
    return BacktestResult(definition.id, evaluated, alerts, labelled, tp, positives, source)


def with_overrides(definition: RuleDef, overrides: Mapping[str, Any]) -> RuleDef:
    """Draft copy of a rule (rule studio "what-if" simulation)."""
    merged = {**definition.to_dict(), **{k: v for k, v in overrides.items() if v is not None}}
    return replace(RuleDef.from_dict(merged), version=definition.version)
