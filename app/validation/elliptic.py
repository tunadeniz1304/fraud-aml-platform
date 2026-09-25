"""Graph-module validation on the Elliptic Bitcoin transaction graph.

Node classification (illicit vs licit; "unknown" nodes stay in the graph but
are not scored) with the standard **temporal split** of Weber et al. (2019):
time steps 1–34 train, 35–49 test.

Feature sets compared with LightGBM:

* ``graph`` — only the platform's structural mule/graph features computed on
  the transaction graph: PageRank, in/out degree (fan-in / fan-out) and weakly
  connected component size.

  The platform's fraud-proximity feature (hop distance to a *known* illicit
  node) is deliberately **not** used: Elliptic's edges never cross time steps,
  so a test node (steps 35–49) can never reach a training label, while a
  training illicit node is its own seed (distance 0) — the feature would only
  encode the training label. A first run with it collapsed the illicit F1 to
  0.13–0.23, which is how the leak was found.
* ``local`` — the 94 local transaction features shipped with the data set;
* ``local+graph`` and ``all`` (all 165 shipped features) ``+graph``.

Protocol (no test-period information reaches training):

* graph features are **inductive**: every node's features are computed on the
  subgraph of its own time step only, so no training feature is computed on a
  graph that contains test-period nodes (``cross_step_edges`` in the output
  confirms that no edge crosses steps, i.e. nothing is lost by this);
* the number of boosting rounds comes from early stopping on a *temporal*
  validation split inside training (steps 1–29 fit, 30–34 validate); the model
  is then refitted on steps 1–34 with that round count;
* the F1 decision threshold comes from expanding-window temporal folds inside
  training (fit on all earlier steps, predict 20–24, 25–29, 30–34);
* test metrics carry stratified bootstrap 95 % intervals.

The literature baselines quoted in docs/VALIDATION_REPORT.md are copied from
Weber et al. (2019), Table 1 / Table 2, same split.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

TRAIN_MAX_STEP = 34
LOCAL_FEATURES = 93  # columns after txId + time step that are "local" (Weber: 94 incl. step)
GRAPH_FEATURES = ("pagerank", "in_degree", "out_degree", "component_size")
EARLY_STOP_VALID_FROM = 30  # training steps 30..34 validate the round count
OOF_FOLDS = ((20, 24), (25, 29), (30, 34))  # expanding-window folds for the threshold
MAX_ROUNDS = 1000
EARLY_STOP_PATIENCE = 50
BOOTSTRAP_ROUNDS = 1000

_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "feature_fraction": 0.8,
    "deterministic": True,
    "verbose": -1,
}


@dataclass
class EllipticData:
    ids: np.ndarray
    step: np.ndarray
    x: np.ndarray  # shipped features (165)
    label: np.ndarray  # 1 illicit, 0 licit, -1 unknown
    edges: np.ndarray  # (m, 2) source → target


def load(directory: Path) -> EllipticData:
    import pandas as pd

    feats = pd.read_csv(directory / "elliptic_txs_features.csv", header=None)
    classes = pd.read_csv(directory / "elliptic_txs_classes.csv")
    edges = pd.read_csv(directory / "elliptic_txs_edgelist.csv")
    cls = dict(zip(classes["txId"], classes["class"].astype(str), strict=True))
    ids = feats[0].to_numpy()
    label = np.asarray([1 if cls.get(i) == "1" else 0 if cls.get(i) == "2" else -1 for i in ids])
    return EllipticData(
        ids=ids,
        step=feats[1].to_numpy(),
        x=feats.iloc[:, 2:].to_numpy(dtype=float),
        label=label,
        edges=edges[["txId1", "txId2"]].to_numpy(),
    )


def cross_step_edges(data: EllipticData) -> int:
    """Edges whose endpoints lie in different time steps (0 on Elliptic)."""
    step = dict(zip(data.ids.tolist(), data.step.tolist(), strict=True))
    return sum(1 for a, b in data.edges.tolist() if step.get(a) != step.get(b))


def graph_features(data: EllipticData) -> np.ndarray:
    """Platform graph features per node, computed on the subgraph of the
    node's own time step only (inductive; see module docstring)."""
    import networkx as nx

    step_of = dict(zip(data.ids.tolist(), data.step.tolist(), strict=True))
    by_step: dict[int, list[tuple[int, int]]] = {}
    for a, b in data.edges.tolist():
        if step_of.get(a) is not None and step_of.get(a) == step_of.get(b):
            by_step.setdefault(int(step_of[a]), []).append((a, b))
    feats: dict[int, list[float]] = {}
    for step in np.unique(data.step).tolist():
        nodes = data.ids[data.step == step].tolist()
        g = nx.DiGraph()
        g.add_nodes_from(nodes)
        g.add_edges_from(by_step.get(int(step), []))
        pagerank = nx.pagerank(g)
        comp_size: dict[int, int] = {}
        for comp in nx.weakly_connected_components(g):
            for n in comp:
                comp_size[n] = len(comp)
        for node in nodes:
            feats[node] = [
                pagerank.get(node, 0.0) * len(nodes),  # comparable across step sizes
                float(g.in_degree(node)),
                float(g.out_degree(node)),
                float(comp_size.get(node, 1)),
            ]
    return np.asarray([feats[n] for n in data.ids.tolist()])


def _rounds(x: np.ndarray, y: np.ndarray, step: np.ndarray, seed: int) -> int:
    """Boosting rounds by early stopping on the last training steps."""
    import lightgbm as lgb

    fit, valid = step < EARLY_STOP_VALID_FROM, step >= EARLY_STOP_VALID_FROM
    booster = lgb.train(
        {**_PARAMS, "seed": seed},
        lgb.Dataset(x[fit], label=y[fit]),
        num_boost_round=MAX_ROUNDS,
        valid_sets=[lgb.Dataset(x[valid], label=y[valid])],
        callbacks=[lgb.early_stopping(EARLY_STOP_PATIENCE, verbose=False)],
    )
    return max(10, int(booster.best_iteration or MAX_ROUNDS))


def _fit_predict(
    x_tr: np.ndarray, y_tr: np.ndarray, x_te: np.ndarray, seed: int, rounds: int
) -> np.ndarray:
    import lightgbm as lgb

    booster = lgb.train(
        {**_PARAMS, "seed": seed}, lgb.Dataset(x_tr, label=y_tr), num_boost_round=rounds
    )
    return np.asarray(booster.predict(x_te))


def _oof(
    x: np.ndarray, y: np.ndarray, step: np.ndarray, seed: int, rounds: int
) -> tuple[np.ndarray, np.ndarray]:
    """Expanding-window temporal out-of-fold predictions inside training, for
    threshold selection without test data. Returns (labels, predictions)."""
    ys, ps = [], []
    for lo, hi in OOF_FOLDS:
        tr, va = step < lo, (step >= lo) & (step <= hi)
        ys.append(y[va])
        ps.append(_fit_predict(x[tr], y[tr], x[va], seed, rounds))
    return np.concatenate(ys), np.concatenate(ps)


def _best_f1_threshold(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import precision_recall_curve

    precision, recall, thresholds = precision_recall_curve(y, p)
    f1 = 2 * precision * recall / np.clip(precision + recall, 1e-12, None)
    return float(thresholds[int(np.argmax(f1[:-1]))]) if len(thresholds) else 0.5


def _bootstrap_ci(
    y: np.ndarray, p: np.ndarray, thr: float, rounds: int, seed: int
) -> dict[str, list[float]]:
    """Stratified percentile bootstrap of illicit F1 (fixed threshold) and PR-AUC."""
    from app.validation.evaluate import _ci, _rank_metrics

    rng = np.random.default_rng(seed)
    pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    f1s, aps = [], []
    for _ in range(rounds):
        idx = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        yb, pb = y[idx], p[idx]
        pred = pb >= thr
        tp = float((pred & (yb == 1)).sum())
        f1s.append(2 * tp / max(1.0, float(pred.sum()) + float(yb.sum())))
        aps.append(_rank_metrics(yb, pb, [1])[0])
    return {"illicit_f1": _ci(f1s), "pr_auc": _ci(aps)}


def evaluate(
    data: EllipticData, *, seed: int = 42, rounds: int = BOOTSTRAP_ROUNDS
) -> dict[str, Any]:
    from sklearn.metrics import average_precision_score, f1_score, precision_score, recall_score

    gf = graph_features(data)
    labelled = data.label >= 0
    train = labelled & (data.step <= TRAIN_MAX_STEP)
    test = labelled & (data.step > TRAIN_MAX_STEP)
    local = data.x[:, :LOCAL_FEATURES]
    sets = {
        "graph": gf,
        "local": local,
        "local+graph": np.hstack([local, gf]),
        "all": data.x,
        "all+graph": np.hstack([data.x, gf]),
    }
    y_tr, y_te = data.label[train], data.label[test]
    step_tr = data.step[train]
    results: dict[str, Any] = {}
    for name, x in sets.items():
        n_rounds = _rounds(x[train], y_tr, step_tr, seed)
        p_te = _fit_predict(x[train], y_tr, x[test], seed, n_rounds)
        # decision threshold chosen on training data only (temporal OOF), never on test
        thr = _best_f1_threshold(*_oof(x[train], y_tr, step_tr, seed, n_rounds))
        pred = (p_te >= thr).astype(int)
        ci = _bootstrap_ci(y_te, p_te, thr, rounds, seed)
        results[name] = {
            "illicit_f1": round(float(f1_score(y_te, pred)), 4),
            "illicit_f1_ci95": ci["illicit_f1"],
            "illicit_precision": round(float(precision_score(y_te, pred, zero_division=0)), 4),
            "illicit_recall": round(float(recall_score(y_te, pred)), 4),
            "illicit_f1_at_0.5": round(float(f1_score(y_te, (p_te >= 0.5).astype(int))), 4),
            "pr_auc": round(float(average_precision_score(y_te, p_te)), 4),
            "pr_auc_ci95": ci["pr_auc"],
            "threshold": round(thr, 4),
            "boosting_rounds": n_rounds,
            "features": int(x.shape[1]),
        }
    return {
        "nodes": len(data.ids),
        "edges": len(data.edges),
        "train": {"nodes": int(train.sum()), "illicit": int(y_tr.sum())},
        "test": {"nodes": int(test.sum()), "illicit": int(y_te.sum())},
        "split": f"zaman adımı 1–{TRAIN_MAX_STEP} eğitim, {TRAIN_MAX_STEP + 1}+ test",
        "results": results,
        "graph_features": list(GRAPH_FEATURES),
        "graph_features_mode": "inductive: her düğüm yalnızca kendi zaman adımının alt grafiğinde",
        "cross_step_edges": cross_step_edges(data),
        "early_stopping": (
            f"adım 1–{EARLY_STOP_VALID_FROM - 1} fit, {EARLY_STOP_VALID_FROM}–{TRAIN_MAX_STEP} "
            f"doğrulama (sabır {EARLY_STOP_PATIENCE}); sonra 1–{TRAIN_MAX_STEP} yeniden fit"
        ),
        "threshold_selection": (
            "eğitim içi genişleyen pencere zamansal katlar: "
            + ", ".join(f"{lo}–{hi}" for lo, hi in OOF_FOLDS)
        ),
        "bootstrap": {"rounds": rounds, "method": "stratified percentile, 95%"},
    }
