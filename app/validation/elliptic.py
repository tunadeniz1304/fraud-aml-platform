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


def graph_features(data: EllipticData) -> np.ndarray:
    """Platform graph features for every node (see module docstring)."""
    import networkx as nx

    g = nx.DiGraph()
    g.add_nodes_from(data.ids.tolist())
    g.add_edges_from(map(tuple, data.edges.tolist()))
    pagerank = nx.pagerank(g)
    comp_size: dict[int, int] = {}
    for comp in nx.weakly_connected_components(g):
        for n in comp:
            comp_size[n] = len(comp)
    rows = [
        [
            pagerank.get(node, 0.0),
            float(g.in_degree(node)),
            float(g.out_degree(node)),
            float(comp_size.get(node, 1)),
        ]
        for node in data.ids.tolist()
    ]
    return np.asarray(rows)


def _fit_predict(x_tr: np.ndarray, y_tr: np.ndarray, x_te: np.ndarray, seed: int) -> np.ndarray:
    import lightgbm as lgb

    params = {
        "objective": "binary",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "feature_fraction": 0.8,
        "seed": seed,
        "deterministic": True,
        "verbose": -1,
    }
    booster = lgb.train(params, lgb.Dataset(x_tr, label=y_tr), num_boost_round=300)
    return np.asarray(booster.predict(x_te))


def _oof(x: np.ndarray, y: np.ndarray, seed: int, folds: int = 3) -> np.ndarray:
    """Out-of-fold training predictions (threshold selection without test data)."""
    from sklearn.model_selection import StratifiedKFold

    out = np.zeros(len(y))
    for tr, va in StratifiedKFold(folds, shuffle=True, random_state=seed).split(x, y):
        out[va] = _fit_predict(x[tr], y[tr], x[va], seed)
    return out


def _best_f1_threshold(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import precision_recall_curve

    precision, recall, thresholds = precision_recall_curve(y, p)
    f1 = 2 * precision * recall / np.clip(precision + recall, 1e-12, None)
    return float(thresholds[int(np.argmax(f1[:-1]))]) if len(thresholds) else 0.5


def evaluate(data: EllipticData, *, seed: int = 42) -> dict[str, Any]:
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
    results: dict[str, Any] = {}
    for name, x in sets.items():
        p_te = _fit_predict(x[train], y_tr, x[test], seed)
        # decision threshold chosen on training data only (out-of-fold), never on test
        thr = _best_f1_threshold(y_tr, _oof(x[train], y_tr, seed))
        pred = (p_te >= thr).astype(int)
        results[name] = {
            "illicit_f1": round(float(f1_score(y_te, pred)), 4),
            "illicit_precision": round(float(precision_score(y_te, pred, zero_division=0)), 4),
            "illicit_recall": round(float(recall_score(y_te, pred)), 4),
            "illicit_f1_at_0.5": round(float(f1_score(y_te, (p_te >= 0.5).astype(int))), 4),
            "pr_auc": round(float(average_precision_score(y_te, p_te)), 4),
            "threshold": round(thr, 4),
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
    }
