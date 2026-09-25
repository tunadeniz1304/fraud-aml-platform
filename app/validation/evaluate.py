"""Time-split evaluation, layer ablation and cost outcome of a shadow replay.

Split: the replay rows are ordered by time. The first 70 % of rows train, the
next 15 % validate (GBM early stopping, anomaly calibrators, stacker) and the
last 15 % test. The boundaries snap to timestamp changes, so no timestamp (e.g.
one PaySim step) straddles two parts. The same 70/15/15 row split is used by
``app.ml.training``.

Layers (all fitted on the train/validation periods only):

1. ``rules`` — the rule DSL score (noisy-OR);
2. ``gbm`` — the LightGBM probability on its own;
3. ``rules+gbm`` — a non-negative logistic stack of rule + LightGBM;
4. ``+anomaly`` — the production stacker (rule, GBM, IForest+ECOD). Its
   validation ablation may drop the anomaly input (see ``app.ml.stacker``);
5. ``+graph`` — the policy's noisy-OR of the stacked probability with the
   burst and entity-graph signal scores (configured weights);
6. ``full`` — the production *decision*: rule action floors, typology caps and
   thresholds give ALLOW / STEP_UP / HOLD / BLOCK. The ranking score is
   ``action index + risk``, so the action tier ranks first and risk breaks ties
   inside a tier. This is the order in which an analyst queue sees the events.
   ``full`` therefore differs from ``+graph`` exactly where floors and caps
   move an event across tiers.

The ablation chain reports each layer's paired-bootstrap PR-AUC difference to
the previous layer. A separate block compares every stacked layer with the GBM
alone. A 95 % interval that contains 0 is reported as "katkı yok". The
bootstrap is **stratified**: positives and negatives are resampled separately,
so every resample keeps the test fraud count. It runs ``BOOTSTRAP_ROUNDS``
resamples.

Budgets (top-k % of test events alerted) come from ``BUDGETS``. Calibration
(Brier, 10-bin ECE) is reported for the layers that output a probability.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np

from app.config import get_settings
from app.ml import metrics as M
from app.ml.stacker import fit_logistic, logits
from app.ml.training import BackfillRow, TrainingConfig, fit_hybrid, model_feature_names
from app.scoring.policy import PolicyEngine, PolicyInput
from app.scoring.rules import ACTIONS
from app.validation.replay import SIGNALS, ReplayResult

LAYERS = ("rules", "gbm", "rules+gbm", "+anomaly", "+graph", "full")
ABLATION_CHAIN = ("rules", "rules+gbm", "+anomaly", "+graph", "full")
#: stacked layers compared explicitly with the GBM alone
VS_GBM = ("rules+gbm", "+anomaly", "+graph", "full")
#: layers whose output is meant to be a probability (calibration is reported)
PROBABILITY_LAYERS = ("gbm", "rules+gbm", "+anomaly", "+graph")
BUDGETS = (0.005, 0.01, 0.02)
BOOTSTRAP_ROUNDS = 1000
TRAIN_SHARE, VALID_SHARE = 0.70, 0.15
#: time-of-day features (PaySim ``step % 24`` artefact ablation)
HOUR_FEATURES = ("hour", "is_night", "night_ratio_7d", "hour_unusual")


@dataclass
class Split:
    train: ReplayResult
    valid: ReplayResult
    test: ReplayResult
    cutoff_ts: float  # first test timestamp
    valid_start_ts: float


def _snap(ts_sorted: np.ndarray, position: int) -> float:
    """Timestamp at which to cut so that row ``position`` starts the next part."""
    position = min(max(position, 1), len(ts_sorted) - 1)
    return float(ts_sorted[position - 1])


def split_bounds(
    ts: np.ndarray, *, train_share: float = TRAIN_SHARE, valid_share: float = VALID_SHARE
) -> tuple[float, float]:
    """``(train_end, valid_end)`` timestamps for a 70/15/15 row split in time
    order; a row belongs to train if ``ts <= train_end``, to test if
    ``ts > valid_end``."""
    s = np.sort(np.asarray(ts, dtype=float))
    n = len(s)
    return _snap(s, int(n * train_share)), _snap(s, int(n * (train_share + valid_share)))


def time_split(
    res: ReplayResult, *, train_share: float = TRAIN_SHARE, valid_share: float = VALID_SHARE
) -> Split:
    t1, t2 = split_bounds(res.ts, train_share=train_share, valid_share=valid_share)
    order = np.argsort(res.ts, kind="stable")
    ts = res.ts[order]
    train, valid, test = order[ts <= t1], order[(ts > t1) & (ts <= t2)], order[ts > t2]
    return Split(res.take(train), res.take(valid), res.take(test), t2, t1)


def _rows(res: ReplayResult, neutral: Sequence[int] = ()) -> list[BackfillRow]:
    feats = res.features.astype(float)
    if len(neutral):
        feats = feats.copy()
        feats[:, list(neutral)] = 0.0
    return [
        BackfillRow("", "", "", float(a), int(y), k, False, f.tolist(), float(r))
        for f, r, y, a, k in zip(feats, res.rule, res.label, res.amount, res.kind, strict=True)
    ]


def _neutral_idx(features: Sequence[str]) -> list[int]:
    names = model_feature_names()
    return [names.index(f) for f in features]


def layer_scores(
    split: Split, *, seed: int = 42, neutralize: Sequence[str] = ()
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Score the test period with every layer (models fitted on train/valid).

    ``neutralize`` sets the named model features to 0 in every split before
    fitting and scoring (feature-group ablation)."""
    neutral = _neutral_idx(neutralize)
    cfg = TrainingConfig(seed=seed, num_boost_round=300, iforest_estimators=100)
    fitted = fit_hybrid(_rows(split.train, neutral), _rows(split.valid, neutral), cfg)
    test, valid = split.test, split.valid
    x_te = np.asarray([r.features for r in _rows(test, neutral)], dtype=float)
    x_va = np.asarray([r.features for r in _rows(valid, neutral)], dtype=float)
    ml_te = np.asarray(fitted.ml(x_te)) if len(x_te) else np.zeros(0)
    an_te = np.asarray(fitted.anomaly(x_te)) if len(x_te) else np.zeros(0)
    ml_va = np.asarray(fitted.ml(x_va))
    coef2, b2 = fit_logistic(logits(np.column_stack([valid.rule, ml_va])), valid.label.astype(int))
    stack2 = 1.0 / (1.0 + np.exp(-(logits(np.column_stack([test.rule, ml_te])) @ coef2 + b2)))
    hybrid = np.asarray(fitted.hybrid(test.rule.tolist(), ml_te.tolist(), an_te.tolist()))
    policy = PolicyEngine(fitted.stacker)
    with_graph = np.asarray(
        [
            policy.combine(h, {k: float(test.signals[k][i]) for k in SIGNALS})
            for i, h in enumerate(hybrid)
        ]
    )
    full, actions = [], []
    for i in range(len(test)):
        decision = policy.decide(
            PolicyInput(
                rule_score=float(test.rule[i]),
                ml_score=float(ml_te[i]),
                anomaly_score=float(an_te[i]),
                signals={k: float(test.signals[k][i]) for k in SIGNALS},
                action_hint=ACTIONS[test.floor[i]] if test.floor[i] >= 0 else None,
                cap=ACTIONS[test.cap[i]] if test.cap[i] >= 0 else None,
                amount_try=float(test.amount[i]),
            )
        )
        full.append(ACTIONS.index(decision.decision) + decision.risk_score)
        actions.append(decision.decision)
    scores = {
        "rules": test.rule.astype(float),
        "gbm": ml_te,
        "rules+gbm": stack2,
        "+anomaly": hybrid,
        "+graph": with_graph,
        "full": np.asarray(full),
    }
    importance = fitted.gbm.feature_importance()
    stacker = fitted.stacker
    assert stacker is not None
    extras = {
        "actions": actions,
        "feature_importance": dict(sorted(importance.items(), key=lambda kv: -kv[1])[:12]),
        "feature_importance_all": importance,
        "trees": fitted.gbm.booster.num_trees(),
        "thresholds": policy.thresholds.as_dict(),
        "stacker": stacker.to_dict(),
        "rules+gbm_coef": [round(float(c), 4) for c in coef2],
    }
    return scores, extras


def _ci(values: Sequence[float]) -> list[float]:
    if not len(values):
        return [math.nan, math.nan]
    lo, hi = np.percentile(values, [2.5, 97.5])
    return [round(float(lo), 4), round(float(hi), 4)]


def _rank_metrics(y: np.ndarray, s: np.ndarray, ks: Sequence[int]) -> tuple[float, float, list]:
    """(average precision, ROC-AUC, recall at top-k for each k) in one sort.

    Average precision matches ``sklearn.metrics.average_precision_score``
    (one point per distinct score)."""
    from scipy.stats import rankdata

    order = np.argsort(-s, kind="stable")
    ys, ss = y[order], s[order]
    tp = np.cumsum(ys)
    pos = float(tp[-1])
    neg = float(len(ys) - pos)
    last = np.r_[np.flatnonzero(np.diff(ss) != 0), len(ss) - 1]
    precision = tp[last] / (last + 1)
    recall = tp[last] / pos
    ap = float(np.sum(np.diff(np.r_[0.0, recall]) * precision))
    ranks = rankdata(s)
    auc = float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))
    return ap, auc, [float(tp[k - 1] / pos) for k in ks]


def bootstrap(
    y: np.ndarray,
    scores: dict[str, np.ndarray],
    *,
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = 7,
    budgets: Sequence[float] = BUDGETS,
    chain: Sequence[str] = ABLATION_CHAIN,
    baseline: str | None = "gbm",
    versus: Sequence[str] = VS_GBM,
) -> dict[str, Any]:
    """Stratified percentile CIs per layer (PR-AUC, ROC-AUC, recall at each
    budget), of consecutive ``chain`` deltas and of ``layer - baseline``."""
    rng = np.random.default_rng(seed)
    y = np.asarray(y, dtype=int)
    pos_idx, neg_idx = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    n = len(y)
    ks = [max(1, round(n * b)) for b in budgets]
    names = list(scores)
    samples: dict[str, dict[str, list[float]]] = {
        k: {"pr_auc": [], "roc_auc": [], **{f"recall_{b:g}": [] for b in budgets}} for k in names
    }
    chain = [c for c in chain if c in scores]
    deltas: dict[str, list[float]] = {k: [] for k in chain[1:]}
    vs: dict[str, list[float]] = {k: [] for k in versus if baseline in scores and k in scores}
    for _ in range(rounds):
        idx = np.concatenate([rng.choice(pos_idx, len(pos_idx)), rng.choice(neg_idx, len(neg_idx))])
        yb = y[idx]
        pr: dict[str, float] = {}
        for name in names:
            ap, auc, recalls = _rank_metrics(yb, scores[name][idx], ks)
            pr[name] = ap
            samples[name]["pr_auc"].append(ap)
            samples[name]["roc_auc"].append(auc)
            for b, r in zip(budgets, recalls, strict=True):
                samples[name][f"recall_{b:g}"].append(r)
        for prev, cur in pairwise(chain):
            deltas[cur].append(pr[cur] - pr[prev])
        for k in vs:
            vs[k].append(pr[k] - pr[str(baseline)])
    out: dict[str, Any] = {k: {m: _ci(v) for m, v in d.items()} for k, d in samples.items()}
    out["delta_pr_auc"] = {k: _ci(v) for k, v in deltas.items()}
    out["vs_baseline_pr_auc"] = {k: _ci(v) for k, v in vs.items()}
    return out


def cost_outcome(
    y: np.ndarray, scores: np.ndarray, amount: np.ndarray, budget: float, review_cost: float
) -> dict[str, float]:
    k = max(1, round(len(scores) * budget))
    top = np.argsort(-scores, kind="stable")[:k]
    fraud_total = float(amount[y == 1].sum())
    caught = float(amount[top][y[top] == 1].sum())
    false_alerts = int((y[top] == 0).sum())
    review = false_alerts * review_cost
    return {
        "alerts": int(k),
        "false_alerts": false_alerts,
        "fraud_amount_total": round(fraud_total, 2),
        "fraud_amount_caught": round(caught, 2),
        "fraud_amount_missed": round(fraud_total - caught, 2),
        "review_cost": round(review, 2),
        "total_cost": round(fraud_total - caught + review, 2),
        "baseline_cost_no_system": round(fraud_total, 2),
    }


def layer_metrics(
    y: np.ndarray, s: np.ndarray, amount: np.ndarray, budgets: Sequence[float] = BUDGETS
) -> dict[str, Any]:
    settings = get_settings()
    out: dict[str, Any] = {
        "pr_auc": round(M.pr_auc(y, s), 4),
        "roc_auc": round(M.roc_auc(y, s), 4),
        "recall_at_1pct_fpr": round(M.recall_at_fpr(y, s, 0.01), 4),
    }
    for budget in budgets:
        b = M.budget_metrics(y, s, amount, budget)
        out[f"budget_{budget:g}"] = {
            "recall": round(b["recall"], 4),
            "precision": round(b["precision"], 4),
            "cost_weighted_recall": round(b["cost_weighted_recall"], 4),
            "cost": cost_outcome(y, s, amount, budget, settings.fp_cost_try),
        }
    return out


def _verdict(delta: float, lo: float, hi: float) -> str:
    return "katkı yok" if lo <= 0 <= hi else ("katkı var" if delta > 0 else "zarar")


@dataclass
class Evaluation:
    report: dict[str, Any]
    split: Split
    scores: dict[str, np.ndarray]


def evaluate_split(
    split: Split,
    *,
    seed: int = 42,
    rounds: int = BOOTSTRAP_ROUNDS,
    budgets: Sequence[float] = BUDGETS,
    feature_ablations: dict[str, Sequence[str]] | None = None,
) -> Evaluation:
    """Full report block for one time split (JSON-serialisable)."""
    settings = get_settings()
    scores, extras = layer_scores(split, seed=seed)
    y, amount = split.test.label.astype(int), split.test.amount
    ci = bootstrap(y, scores, rounds=rounds, budgets=budgets)
    layers = {}
    for name in LAYERS:
        layers[name] = layer_metrics(y, scores[name], amount, budgets)
        layers[name]["pr_auc_ci95"] = ci[name]["pr_auc"]
        layers[name]["roc_auc_ci95"] = ci[name]["roc_auc"]
        for b in budgets:
            layers[name][f"budget_{b:g}"]["recall_ci95"] = ci[name][f"recall_{b:g}"]
    ablation = []
    for prev, cur in pairwise(ABLATION_CHAIN):
        lo, hi = ci["delta_pr_auc"][cur]
        delta = layers[cur]["pr_auc"] - layers[prev]["pr_auc"]
        ablation.append(
            {
                "layer": cur,
                "delta_pr_auc": round(delta, 4),
                "ci95": [lo, hi],
                "verdict": _verdict(delta, lo, hi),
            }
        )
    vs_gbm = []
    for cur in VS_GBM:
        lo, hi = ci["vs_baseline_pr_auc"][cur]
        delta = layers[cur]["pr_auc"] - layers["gbm"]["pr_auc"]
        vs_gbm.append(
            {
                "layer": cur,
                "delta_pr_auc_vs_gbm": round(delta, 4),
                "ci95": [lo, hi],
                "verdict": _verdict(delta, lo, hi),
            }
        )
    calibration = {
        name: M.calibration(y, np.clip(scores[name], 0, 1)) for name in PROBABILITY_LAYERS
    }
    actions = extras["actions"]
    dist = {a: int(sum(1 for x in actions if x == a)) for a in ACTIONS}
    fraud_dist = {
        a: int(sum(1 for x, yy in zip(actions, y, strict=True) if x == a and yy == 1))
        for a in ACTIONS
    }
    report: dict[str, Any] = {
        "split": {
            "method": "zaman sıralı satır bölmesi 70/15/15 (sınırlar zaman damgası değişiminde)",
            "valid_start_ts": split.valid_start_ts,
            "test_start_ts": split.cutoff_ts,
        },
        "rows": {
            "total": len(split.train) + len(split.valid) + len(split.test),
            "train": len(split.train),
            "valid": len(split.valid),
            "test": len(split.test),
            "train_fraud": int(split.train.label.sum()),
            "valid_fraud": int(split.valid.label.sum()),
            "test_fraud": int(y.sum()),
            "test_fraud_rate": round(float(y.mean()), 5),
        },
        "layers": layers,
        "ablation": ablation,
        "stacked_vs_gbm": vs_gbm,
        "calibration_test": calibration,
        "stacker": extras["stacker"],
        "rules+gbm_coef": extras["rules+gbm_coef"],
        "actions": {"all": dist, "fraud": fraud_dist},
        "feature_importance_top": extras["feature_importance"],
        "thresholds": extras["thresholds"],
        "model": {"features": len(model_feature_names()), "trees": extras["trees"]},
        "review_cost_try": settings.fp_cost_try,
        "bootstrap": {"rounds": rounds, "method": "stratified percentile (pos/neg ayrı)"},
        "budgets": list(budgets),
    }
    if feature_ablations:
        report["feature_ablation"] = {
            label: _feature_ablation(split, list(feats), scores, extras, seed, rounds, budgets)
            for label, feats in feature_ablations.items()
        }
    return Evaluation(report, split, scores)


def _feature_ablation(
    split: Split,
    features: list[str],
    base: dict[str, np.ndarray],
    base_extras: dict[str, Any],
    seed: int,
    rounds: int,
    budgets: Sequence[float],
) -> dict[str, Any]:
    """Refit without ``features`` and compare GBM / stacker with the full fit."""
    scores, extras = layer_scores(split, seed=seed, neutralize=features)
    y, amount = split.test.label.astype(int), split.test.amount
    pair = {
        "gbm": base["gbm"],
        "gbm_without": scores["gbm"],
        "+anomaly": base["+anomaly"],
        "+anomaly_without": scores["+anomaly"],
    }
    ci = bootstrap(
        y,
        pair,
        rounds=rounds,
        budgets=budgets,
        chain=(),
        baseline=None,
        versus=(),
    )
    diff = bootstrap(
        y,
        {"gbm": pair["gbm"], "gbm_without": pair["gbm_without"]},
        rounds=rounds,
        budgets=budgets,
        chain=("gbm", "gbm_without"),
    )
    imp = base_extras["feature_importance_all"]
    out: dict[str, Any] = {
        "features": features,
        "importance_share_full_model": round(sum(imp.get(f, 0.0) for f in features), 4),
        "delta_pr_auc_gbm_ci95": diff["delta_pr_auc"]["gbm_without"],
        "top_features_without": extras["feature_importance"],
    }
    for name, s in pair.items():
        m = layer_metrics(y, s, amount, budgets)
        out[name] = {
            "pr_auc": m["pr_auc"],
            "pr_auc_ci95": ci[name]["pr_auc"],
            **{
                f"recall_{b:g}": [m[f"budget_{b:g}"]["recall"], ci[name][f"recall_{b:g}"]]
                for b in budgets
            },
        }
    return out


def evaluate_replay(
    res: ReplayResult,
    *,
    seed: int = 42,
    rounds: int = BOOTSTRAP_ROUNDS,
    feature_ablations: dict[str, Sequence[str]] | None = None,
) -> Evaluation:
    evaluation = evaluate_split(
        time_split(res), seed=seed, rounds=rounds, feature_ablations=feature_ablations
    )
    evaluation.report["replay_seconds"] = round(res.seconds, 1)
    return evaluation
