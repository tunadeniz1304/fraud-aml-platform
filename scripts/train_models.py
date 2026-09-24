"""Train the hybrid fraud models on the synthetic dataset (P0.7).

    python scripts/train_models.py                  # veri yoksa üretir, fraud_gbm_v1 (champion)
    python scripts/train_models.py --version fraud_gbm_v2 --status challenger --seed 7
    python scripts/train_models.py --incremental    # DB'deki analist etiketleriyle yeni versiyon

Writes ``models/<version>/`` (model, anomaly arrays, stacker, metadata,
``model_card.md``), updates ``models/registry.json`` and prints the metrics.
``--incremental`` overlays analyst labels (``labels`` table) on the synthetic
labels before training (feedback loop, P1.6).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.ml.training import TrainingConfig, train
from app.synthetic.generator import SyntheticConfig, generate, load_dataset


async def _db_labels() -> dict[str, int]:
    from sqlalchemy import select

    from app.db.database import Database
    from app.db.models import Label

    db = Database(get_settings().resolved_database_url)
    try:
        async with db.session() as session:
            rows = (await session.execute(select(Label.transaction_id, Label.label))).all()
    finally:
        await db.dispose()
    return {tx: int(label) for tx, label in rows}


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--version", default="fraud_gbm_v1")
    parser.add_argument("--status", default="champion", choices=["champion", "challenger"])
    parser.add_argument("--data", type=Path, default=settings.data_dir / "generated")
    parser.add_argument("--models", type=Path, default=settings.resolved_models_dir)
    parser.add_argument("--incremental", action="store_true")
    args = parser.parse_args(argv)

    if not (args.data / "transactions.jsonl").is_file():
        print(f"Sentetik veri yok — üretiliyor: {args.data}")
        generate(SyntheticConfig(seed=args.seed)).write(args.data)
    customers, transactions = load_dataset(args.data)
    manifest: dict[str, Any] = {}
    if (args.data / "manifest.json").is_file():
        manifest = json.loads((args.data / "manifest.json").read_text(encoding="utf-8"))
        manifest.pop("rings", None)
    if args.incremental:
        overrides = asyncio.run(_db_labels())
        hit = 0
        for tx in transactions:
            if tx["transaction_id"] in overrides:
                tx["label"] = overrides[tx["transaction_id"]]
                hit += 1
        manifest["analyst_labels_applied"] = hit
        print(f"Analist etiketleri uygulandı: {hit}")

    cfg = TrainingConfig(
        seed=args.seed,
        version=args.version,
        models_dir=args.models,
        status=args.status,
        backtest_path=args.data / "backtest.jsonl",
        data_manifest=manifest,
    )
    report = train(customers, transactions, cfg)
    print("OK", report.headline())
    print(json.dumps(report.metrics["test"]["hybrid"], ensure_ascii=False))
    print("Model kartı:", report.path / "model_card.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
