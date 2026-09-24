"""Generate the seeded synthetic dataset (P0.7).

    python scripts/generate_synthetic.py                      # 500 müşteri, 60 gün
    python scripts/generate_synthetic.py --seed 7 --customers 200 --days 30

Output (gitignored): ``data/generated/{customers.json, transactions.jsonl,
payees.json, manifest.json}``. Same seed -> identical files.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.synthetic.generator import SyntheticConfig, generate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--customers", type=int, default=500)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--out", type=Path, default=get_settings().data_dir / "generated")
    args = parser.parse_args(argv)

    dataset = generate(SyntheticConfig(seed=args.seed, customers=args.customers, days=args.days))
    paths = dataset.write(args.out)
    digest = hashlib.sha256(paths["transactions"].read_bytes()).hexdigest()[:16]
    summary = dataset.summary()
    print(
        f"OK seed={summary['seed']} müşteri={summary['customers']} "
        f"işlem={summary['transactions']} fraud={summary['fraud']} "
        f"({summary['fraud_rate'] * 100:.2f}%) sha256={digest}"
    )
    print("Tipolojiler:", summary["typologies"])
    print("Çıktı:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
