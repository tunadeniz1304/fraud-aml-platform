"""Time-split evaluation, layer ablation and cost outcome of a shadow replay.

Layers (each adds one component to the previous one, all fitted on the
training period only):

1. ``rules`` — rule DSL score (noisy-OR);
2. ``rules+gbm`` — logistic stack of rule + LightGBM probability;
3. ``+anomaly`` — the production stacker (rule, GBM, IForest+ECOD);
4. ``+graph`` — the policy's noisy-OR combination with the burst and
   entity-graph signal scores (configured weights);
5. ``full`` — the complete policy: rule action floors, typology caps and
   thresholds → risk score and ALLOW / STEP_UP / HOLD / BLOCK.

Every layer's contribution is the paired-bootstrap difference in PR-AUC to the
previous layer; a 95 % interval that contains 0 is reported as "katkı yok".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np

from app.config import get_settings
from app.ml import metrics as M
from app.ml.stacker import logit
from app.ml.training import BackfillRow, TrainingConfig, fit_hybrid, model_feature_names
from app.scoring.policy import PolicyEngine, PolicyInput
from app.scoring.rules import ACTIONS
from app.validation.replay import SIGNALS, ReplayResult

LAYERS = ("rules", "gbm", "rules+gbm", "+anomaly", "+graph", "full")
ABLATION_CHAIN = ("rules", "rules+gbm", "+anomaly", "+graph", "full")


@dataclass
class Split:
    train: ReplayResult
    valid: ReplayResult
    test: ReplayResult
    cutoff_ts: float


def time_split(res: ReplayResult, *, train_share: float = 0.70, valid_share: float = 0.15) -> Split:
    """First ``train_share`` of the *time span* trains (its last ``valid_share``
    of rows validates / stacks), the remaining time span tests."""
    lo, hi = float(res.ts.min()), float(res.ts.max())
    cutoff = lo + train_share * (hi - lo)
    before = np.flatnonzero(res.ts <= cutoff)
    after = np.flatnonzero(res.ts > cutoff)
    n_valid = max(1, int(len(before) * valid_share))
    return Split(res.take(before[:-n_valid]), res.take(before[-n_valid:]), res.take(after), cutoff)


def _rows(res: ReplayResult) -> list[BackfillRow]:
    return [
        BackfillRow("", "", "", float(a), int(y), k, False, f.tolist(), float(r))
        for f, r, y, a, k in zip(
            res.features, res.rule, res.label, res.amount, res.kind, strict=True
        )
    ]


def _stack2(rule: np.ndarray, ml: np.ndarray, y: np.ndarray, seed: int) -> Any:
    from sklearn.linear_model import LogisticRegression

    x = np.column_stack([[logit(v) for v in rule], [logit(v) for v in ml]])
    return LogisticRegression(max_iter=1000, random_state=seed).fit(x, y)


def layer_scores(split: Split, *, seed: int = 42) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Score the test period with every layer (models fitted on train/valid)."""
    cfg = TrainingConfig(seed=seed, num_boost_round=300, iforest_estimators=100)
    fitted = fit_hybrid(_rows(split.train), _rows(split.valid), cfg)
    test, valid = split.test, split.valid
    x_te = test.features.astype(float)
    ml_te = np.asarray(fitted.ml(x_te))
    an_te = np.asarray(fitted.anomaly(x_te))
    ml_va = np.asarray(fitted.ml(valid.features.astype(float)))
    stack2 = _stack2(valid.rule, ml_va, valid.label, seed)
    x2 = np.column_stack([[logit(v) for v in test.rule], [logit(v) for v in ml_te]])
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
        full.append(decision.risk_score)
        actions.append(decision.decision)
    scores = {
        "rules": test.rule,
        "gbm": ml_te,
        "rules+gbm": stack2.predict_proba(x2)[:, 1],
        "+anomaly": hybrid,
        "+graph": with_graph,
        "full": np.asarray(full),
    }
    importance = fitted.gbm.feature_importance()
    extras = {
        "actions": actions,
        "feature_importance": dict(sorted(importance.items(), key=lambda kv: -kv[1])[:12]),
        "trees": fitted.gbm.booster.num_trees(),
        "thresholds": policy.thresholds.as_dict(),
    }
    return scores, extras


def _ci(values: list[float]) -> list[float]:
    if not values:
        return [math.nan, math.nan]
    lo, hi = np.percentile(values, [2.5, 97.5])
    return [round(float(lo), 4), round(float(hi), 4)]


def bootstrap(
    y: np.ndarray, scores: dict[str, np.ndarray], *, rounds: int, seed: int = 7
) -> dict[str, dict[str, list[float]]]:
    """Percentile CIs of PR-AUC / ROC-AUC per layer and of consecutive deltas."""
    rng = np.random.default_rng(seed)
    n = len(y)
    samples: dict[str, dict[str, list[float]]] = {k: {"pr_auc": [], "roc_auc": []} for k in scores}
    deltas: dict[str, list[float]] = {k: [] for k in ABLATION_CHAIN[1:]}
    for _ in range(rounds):
        idx = rng.integers(0, n, n)
        yb = y[idx]
        if yb.min() == yb.max():
            continue
        pr = {}
        for name, s in scores.items():
            pr[name] = M.pr_auc(yb, s[idx])
            samples[name]["pr_auc"].append(pr[name])
            samples[name]["roc_auc"].append(M.roc_auc(yb, s[idx]))
        for prev, cur in pairwise(ABLATION_CHAIN):
            deltas[cur].append(pr[cur] - pr[prev])
    out = {k: {m: _ci(v) for m, v in d.items()} for k, d in samples.items()}
    out["delta_pr_auc"] = {k: _ci(v) for k, v in deltas.items()}
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


def layer_metrics(y: np.ndarray, s: np.ndarray, amount: np.ndarray) -> dict[str, Any]:
    settings = get_settings()
    out: dict[str, Any] = {
        "pr_auc": round(M.pr_auc(y, s), 4),
        "roc_auc": round(M.roc_auc(y, s), 4),
        "recall_at_1pct_fpr": round(M.recall_at_fpr(y, s, 0.01), 4),
    }
    for budget in settings.validation_budgets:
        b = M.budget_metrics(y, s, amount, budget)
        out[f"budget_{budget:g}"] = {
            "recall": round(b["recall"], 4),
            "precision": round(b["precision"], 4),
            "cost_weighted_recall": round(b["cost_weighted_recall"], 4),
            "cost": cost_outcome(y, s, amount, budget, settings.fp_cost_try),
        }
    return out


def evaluate_replay(res: ReplayResult, *, seed: int = 42) -> dict[str, Any]:
    """Full report block for one dataset (JSON-serialisable)."""
    settings = get_settings()
    split = time_split(res)
    scores, extras = layer_scores(split, seed=seed)
    y, amount = split.test.label.astype(int), split.test.amount
    ci = bootstrap(y, scores, rounds=settings.validation_bootstrap_rounds)
    layers = {}
    for name in LAYERS:
        layers[name] = layer_metrics(y, scores[name], amount)
        layers[name]["pr_auc_ci95"] = ci[name]["pr_auc"]
        layers[name]["roc_auc_ci95"] = ci[name]["roc_auc"]
    ablation = []
    for prev, cur in pairwise(ABLATION_CHAIN):
        lo, hi = ci["delta_pr_auc"][cur]
        delta = layers[cur]["pr_auc"] - layers[prev]["pr_auc"]
        verdict = "katkı yok" if lo <= 0 <= hi else ("katkı var" if delta > 0 else "zarar")
        ablation.append(
            {"layer": cur, "delta_pr_auc": round(delta, 4), "ci95": [lo, hi], "verdict": verdict}
        )
    actions = extras["actions"]
    dist = {a: int(sum(1 for x in actions if x == a)) for a in ACTIONS}
    fraud_dist = {
        a: int(sum(1 for x, yy in zip(actions, y, strict=True) if x == a and yy == 1))
        for a in ACTIONS
    }
    return {
        "rows": {
            "total": len(res),
            "train": len(split.train),
            "valid": len(split.valid),
            "test": len(split.test),
            "test_fraud": int(y.sum()),
            "test_fraud_rate": round(float(y.mean()), 5),
        },
        "layers": layers,
        "ablation": ablation,
        "actions": {"all": dist, "fraud": fraud_dist},
        "feature_importance_top": extras["feature_importance"],
        "thresholds": extras["thresholds"],
        "model": {"features": len(model_feature_names()), "trees": extras["trees"]},
        "replay_seconds": round(res.seconds, 1),
        "review_cost_try": settings.fp_cost_try,
        "bootstrap_rounds": settings.validation_bootstrap_rounds,
    }
