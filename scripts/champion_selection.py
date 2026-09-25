"""Evidence-based champion selection (docs/VALIDATION_REPORT.md §7).

    python scripts/champion_selection.py            # train, compare, promote (four eyes)
    python scripts/champion_selection.py --no-promote

1. Regenerates the de-fingerprinted synthetic data (seed 42) and trains two
   candidates with the production training code: ``fraud_gbm_v3`` (seed 42,
   300 rounds) and ``fraud_gbm_v4`` (seed 7, 500 rounds, 200 isolation trees).
2. Compares them on (a) the synthetic time-split holdout and (b) the PaySim
   shadow replay (test period of the cached 10 % replay; the models are
   applied to PaySim's features without retraining — a transfer check).
   Selection rule: lower cost at a 1 % alert budget on the synthetic holdout
   (missed fraud amount + review cost of false alerts), PaySim cost as the
   tie-breaker; recorded in ``artifacts/validation/champion_selection.json``.
3. Promotes the winner through the real API: an admin requests
   MODEL_PROMOTE, a *different* senior analyst approves it (maker-checker);
   the loser stays as challenger (shadow scoring).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from app.config import get_settings
from app.ml import metrics as M
from app.ml.registry import ModelRegistry
from app.ml.training import TrainingConfig, model_feature_names, train
from app.synthetic.generator import SyntheticConfig, generate
from app.validation.evaluate import cost_outcome, time_split
from app.validation.replay import ReplayResult

CANDIDATES = {
    "fraud_gbm_v3": {"seed": 42, "num_boost_round": 300, "iforest_estimators": 100},
    "fraud_gbm_v4": {"seed": 7, "num_boost_round": 500, "iforest_estimators": 200},
}
PAYSIM_CACHE = ROOT / "data" / "external" / "cache" / "paysim_0.1_1_10000.npz"
OUT = ROOT / "artifacts" / "validation" / "champion_selection.json"


def _paysim_block(registry: ModelRegistry, version: str) -> dict[str, Any] | None:
    if not PAYSIM_CACHE.exists():
        return None
    test = time_split(ReplayResult.load(PAYSIM_CACHE)).test
    bundle = registry.load(version)
    names = model_feature_names()
    rows = [dict(zip(names, map(float, f), strict=True)) for f in test.features]
    scores = []
    for rule, feats in zip(test.rule, rows, strict=True):
        s = bundle.score(feats, explain=False)
        anomaly = s.anomaly if s.anomaly is not None else 0.5
        scores.append(bundle.stacker.predict(float(rule), s.ml, anomaly))
    y, amount = test.label.astype(int), test.amount
    fp = get_settings().fp_cost_try
    return {
        "pr_auc": round(M.pr_auc(y, scores), 4),
        "roc_auc": round(M.roc_auc(y, scores), 4),
        "cost_1pct": cost_outcome(y, np.asarray(scores), amount, 0.01, fp),
    }


def _synthetic_block(report: Any) -> dict[str, Any]:
    h = report.metrics["test"]["hybrid"]
    return {
        "pr_auc": h["pr_auc"],
        "roc_auc": h["roc_auc"],
        "recall_at_1pct_fpr": h["recall_at_1pct_fpr"],
        "cost_weighted_recall_1pct": h["budget_1pct"]["cost_weighted_recall"],
    }


def _synthetic_cost(registry: ModelRegistry, version: str) -> float:
    meta = json.loads((registry.root / version / "metadata.json").read_text(encoding="utf-8"))
    budget = meta["metrics"]["test"]["hybrid"]["budget_1pct"]
    return float(1.0 - budget["cost_weighted_recall"])  # share of fraud amount missed


def promote_with_four_eyes(version: str, challenger: str) -> dict[str, Any]:
    """Maker (admin) requests, checker (senior analyst) approves — via the API."""
    from fastapi.testclient import TestClient

    tmp = tempfile.mkdtemp(prefix="anil3-promote-")
    os.environ.update(
        FRAUD_DB_PATH=str(Path(tmp) / "promote.db"),
        STREAM_MODE="off",
        LLM_MODE="demo",
        VECTOR_STORE="off",
        SEED_DEMO_USERS="true",
        RING_DETECT_INTERVAL_S="0",
    )
    get_settings.cache_clear()
    from app.api.dashboard import create_app

    def login(client: TestClient, user: str, password: str) -> dict[str, str]:
        token = client.post("/api/auth/login", json={"username": user, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    s = get_settings()
    with TestClient(create_app()) as client:
        admin = login(client, "admin", s.demo_pw_admin.get_secret_value())
        senior = login(client, "kidemli_analist", s.demo_pw_kidemli.get_secret_value())
        request = client.post(f"/api/models/{version}/promote", headers=admin)
        request.raise_for_status()
        approval = request.json()
        self_approve = client.post(f"/api/approvals/{approval['id']}/approve", headers=admin)
        decided = client.post(
            f"/api/approvals/{approval['id']}/approve",
            headers=senior,
            json={"note": "champion_selection.json kanıtına göre"},
        )
        decided.raise_for_status()
        shadow = client.put(f"/api/models/{challenger}/challenger", headers=admin)
        shadow.raise_for_status()
    return {
        "approval_id": approval["id"],
        "requested_by": "admin",
        "approved_by": "kidemli_analist",
        "self_approval_status": self_approve.status_code,  # 403: maker-checker enforced
        "result": decided.json().get("status"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-promote", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    registry = ModelRegistry(settings.resolved_models_dir)
    data_dir = settings.data_dir / "generated"
    data = generate(SyntheticConfig(seed=42))
    data.write(data_dir)
    manifest = data.summary()
    results: dict[str, Any] = {}
    for version, params in CANDIDATES.items():
        cfg = TrainingConfig(
            version=version,
            models_dir=settings.resolved_models_dir,
            status="challenger",
            backtest_path=data_dir / "backtest.jsonl",
            data_manifest=manifest,
            **params,
        )
        report = train(data.customers, data.transactions, cfg)
        print("OK", report.headline())
        results[version] = {
            "params": params,
            "synthetic_holdout": _synthetic_block(report),
            "synthetic_missed_amount_share_1pct": round(_synthetic_cost(registry, version), 4),
            "paysim_replay": _paysim_block(registry, version),
        }

    def key(v: str) -> tuple[float, float]:
        r = results[v]
        paysim = r["paysim_replay"]["cost_1pct"]["total_cost"] if r["paysim_replay"] else 0.0
        return (r["synthetic_missed_amount_share_1pct"], paysim)

    winner = min(results, key=key)
    loser = next(v for v in results if v != winner)
    decision: dict[str, Any] = {
        "rule": "1 % alarm bütçesinde sentetik holdout'ta kaçan fraud tutarı payı en düşük "
        "aday; eşitlikte PaySim replay toplam maliyeti",
        "winner": winner,
        "challenger": loser,
    }
    if not args.no_promote:
        decision["four_eyes"] = promote_with_four_eyes(winner, loser)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "dataset": "champion/challenger",
                "candidates": results,
                "decision": decision,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(decision, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
