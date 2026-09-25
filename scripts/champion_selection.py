"""Evidence-based champion selection (docs/VALIDATION_REPORT.md §7).

    python scripts/champion_selection.py            # train, compare, promote (four eyes)
    python scripts/champion_selection.py --no-promote

1. Regenerates the de-fingerprinted synthetic data (seed 42) and trains two
   candidates with the production training code: ``fraud_gbm_v5`` (seed 42,
   300 rounds) and ``fraud_gbm_v6`` (seed 7, 500 rounds, 200 isolation trees).
2. Selects on the **validation period only**: the lower share of fraud amount
   missed at a 1 % alert budget, higher validation PR-AUC as the tie-breaker.
   The test period is never looked at for the decision.
3. After the decision, reports each candidate once on (a) the synthetic test
   period and (b) the PaySim shadow replay (test period of the cached 10 %
   replay, models applied without retraining — a transfer check). Recorded
   in ``artifacts/validation/champion_selection.json``.
4. Promotes the winner through the real API: an admin requests
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
    "fraud_gbm_v5": {"seed": 42, "num_boost_round": 300, "iforest_estimators": 100},
    "fraud_gbm_v6": {"seed": 7, "num_boost_round": 500, "iforest_estimators": 200},
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


def _block(h: dict[str, Any]) -> dict[str, Any]:
    return {
        "pr_auc": h["pr_auc"],
        "roc_auc": h["roc_auc"],
        "recall_at_1pct_fpr": h["recall_at_1pct_fpr"],
        "cost_weighted_recall_1pct": h["budget_1pct"]["cost_weighted_recall"],
        # share of fraud amount missed when the riskiest 1 % are alerted
        "missed_amount_share_1pct": round(1.0 - h["budget_1pct"]["cost_weighted_recall"], 4),
    }


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
    tests: dict[str, Any] = {}
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
            "synthetic_validation": _block(report.metrics["valid"]["hybrid"]),
        }
        tests[version] = report.metrics["test"]

    def key(v: str) -> tuple[float, float]:
        val = results[v]["synthetic_validation"]
        return (val["missed_amount_share_1pct"], -val["pr_auc"])

    winner = min(results, key=key)  # validation metrics only
    loser = next(v for v in results if v != winner)
    # reported once, after the decision; never used for it
    for version in results:
        results[version]["reported_after_selection"] = {
            "synthetic_test": {
                **_block(tests[version]["hybrid"]),
                "calibration": tests[version]["calibration"]["hybrid"],
            },
            "paysim_replay": _paysim_block(registry, version),
        }
    decision: dict[str, Any] = {
        "rule": "yalnız doğrulama dönemi: %1 alarm bütçesinde kaçan fraud tutarı payı en düşük "
        "aday; eşitlikte doğrulama PR-AUC'si yüksek olan. Test ve PaySim sonuçları karardan "
        "sonra bir kez raporlanır, seçimde kullanılmaz.",
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
