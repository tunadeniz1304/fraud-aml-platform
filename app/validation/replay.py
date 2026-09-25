"""Shadow replay of a labelled stream through the real detection pipeline.

The events pass **in time order** through the same components production uses
— feature store (in memory), rule DSL, external signals (burst, entity graph)
and the policy layer — without touching any account ("gölge replay"). The
per-event outputs are kept in compact numpy arrays so the 6.3 M-row PaySim file
can be replayed in chunks.

Profile learning follows :func:`app.features.learning.should_learn` with the
step-up outcome simulated from the label (identical to the training backfill),
so the features are the ones the live engine would compute (online/offline
parity). Analyst fraud labels are **not** fed back into the graph during the
replay: that would leak the test labels.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from app.core.sanctions import SanctionScreener
from app.features.extractor import CustomerDirectory, FeatureExtractor
from app.features.learning import simulated_feedback
from app.features.store import MemoryFeatureStore
from app.graph.entity_graph import EntityGraph
from app.graph.signal import GraphSignal
from app.ml.training import model_feature_names
from app.scoring.engine import ScoringEngine
from app.scoring.policy import PolicyEngine
from app.scoring.rules import ACTIONS, RuleSet
from app.scoring.signals import BurstSignal

SIGNALS = ("graph", "burst")


@dataclass
class ReplayResult:
    """Column-oriented replay output (one row per event, time ordered)."""

    features: np.ndarray  # (n, n_features) float32 — model inputs
    rule: np.ndarray  # rule score (noisy-OR)
    floor: np.ndarray  # rule action floor as index in ACTIONS (-1 none)
    cap: np.ndarray  # typology cap as index in ACTIONS (-1 none)
    signals: dict[str, np.ndarray]
    label: np.ndarray
    amount: np.ndarray
    ts: np.ndarray  # epoch seconds
    kind: list[str]  # typology / PaySim type
    seconds: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.label.shape[0])

    def save(self, path: Path) -> None:
        """Cache the replay (re-evaluation without re-running the pipeline)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays: dict[str, np.ndarray] = {
            "features": self.features,
            "rule": self.rule,
            "floor": self.floor,
            "cap": self.cap,
            "label": self.label,
            "amount": self.amount,
            "ts": self.ts,
            "kind": np.asarray(self.kind),
            "seconds": np.asarray([self.seconds]),
            **{f"signal_{k}": v for k, v in self.signals.items()},
        }
        np.savez_compressed(path, **arrays)  # type: ignore[arg-type]

    @classmethod
    def load(cls, path: Path) -> ReplayResult:
        data = np.load(path, allow_pickle=False)
        return cls(
            features=data["features"],
            rule=data["rule"],
            floor=data["floor"],
            cap=data["cap"],
            signals={k: data[f"signal_{k}"] for k in SIGNALS},
            label=data["label"],
            amount=data["amount"],
            ts=data["ts"],
            kind=[str(k) for k in data["kind"]],
            seconds=float(data["seconds"][0]),
        )

    def take(self, idx: np.ndarray) -> ReplayResult:
        return ReplayResult(
            self.features[idx],
            self.rule[idx],
            self.floor[idx],
            self.cap[idx],
            {k: v[idx] for k, v in self.signals.items()},
            self.label[idx],
            self.amount[idx],
            self.ts[idx],
            [self.kind[i] for i in idx.tolist()],
            self.seconds,
            dict(self.meta),
        )


def _action_index(action: str | None) -> int:
    return ACTIONS.index(action) if action in ACTIONS else -1


def build_replay_engine(
    customers: Sequence[dict[str, Any]],
    ruleset: RuleSet,
    *,
    accounts_are_customer_ids: bool = False,
) -> ScoringEngine:
    """Model-free engine with burst + entity-graph signals.

    ``accounts_are_customer_ids`` links a beneficiary id to the customer with
    the same id (PaySim's ``nameDest`` reuses ``C…`` customer ids), so money
    received by a customer and sent on is visible as pass-through.
    """
    directory = CustomerDirectory(customers)
    graph = EntityGraph()
    for record in customers:
        cid = str(record["customer_id"])
        iban = cid if accounts_are_customer_ids else str(record.get("iban") or "")
        graph.register_customer(cid, str(record.get("name", "")), iban)
    extractor = FeatureExtractor(MemoryFeatureStore(), directory)
    return ScoringEngine(
        extractor,
        ruleset=ruleset,
        policy=PolicyEngine(None),
        sanctions=SanctionScreener(),
        signals=[BurstSignal(), GraphSignal(graph)],
        graph=graph,
    )


def _epoch(ts: Any) -> float:
    return (datetime.fromisoformat(ts) if isinstance(ts, str) else ts).timestamp()


async def replay(
    transactions: Iterable[dict[str, Any]],
    engine: ScoringEngine,
    *,
    expected: int | None = None,
    progress_every: int = 0,
) -> ReplayResult:
    """Score + commit every event in order and collect the layer inputs."""
    names = model_feature_names()
    started = time.perf_counter()
    feats: list[np.ndarray] = []
    buf: list[list[float]] = []
    cols: dict[str, list[float]] = {k: [] for k in ("rule", "floor", "cap", "label", "amount")}
    ts_col: list[float] = []
    sig: dict[str, list[float]] = {k: [] for k in SIGNALS}
    kinds: list[str] = []
    for i, tx in enumerate(transactions):
        result = await engine.score(tx)
        assert result.extraction is not None and result.rules is not None
        features = result.features
        buf.append([features[n] for n in names])
        if len(buf) >= 50_000:
            feats.append(np.asarray(buf, dtype=np.float32))
            buf = []
        label = int(tx.get("label", 0))
        cols["rule"].append(result.rules.score)
        cols["floor"].append(_action_index(result.rules.action_hint))
        cap = engine.typology_cap(result.rules, result.signals, features)
        cols["cap"].append(_action_index(cap))
        cols["label"].append(label)
        cols["amount"].append(result.amount_try)
        ts_col.append(_epoch(tx["ts"]))
        for name in SIGNALS:
            sr = result.signals.get(name)
            sig[name].append(sr.score if sr is not None else 0.0)
        kinds.append(str(tx.get("typology") or ""))
        await engine.commit(result, feedback=simulated_feedback(result.decision, label))
        if progress_every and (i + 1) % progress_every == 0:
            rate = (i + 1) / (time.perf_counter() - started)
            total = f"/{expected}" if expected else ""
            print(f"  replay {i + 1}{total} olay ({rate:,.0f} olay/sn)", flush=True)
    if buf:
        feats.append(np.asarray(buf, dtype=np.float32))
    matrix = np.vstack(feats) if feats else np.zeros((0, len(names)), dtype=np.float32)
    return ReplayResult(
        features=matrix,
        rule=np.asarray(cols["rule"]),
        floor=np.asarray(cols["floor"], dtype=np.int8),
        cap=np.asarray(cols["cap"], dtype=np.int8),
        signals={k: np.asarray(v) for k, v in sig.items()},
        label=np.asarray(cols["label"], dtype=np.int8),
        amount=np.asarray(cols["amount"]),
        ts=np.asarray(ts_col),
        kind=kinds,
        seconds=time.perf_counter() - started,
    )
