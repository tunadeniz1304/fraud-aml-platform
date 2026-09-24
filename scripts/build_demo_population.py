"""Build the committed demo population used by ``docker compose up``.

    python scripts/build_demo_population.py   # -> data/demo/{customers,payees,transactions}.json

150 synthetic customers (IBANs, birth years, typing profiles), their payee
registry (for CoP) and ~6 days of *normal* warm-up history (no injected
fraud — attacks come live from the scenario injector / simulator). The
warm-up is rebased to "now" at start-up (``BATCH_REBASE_TS=true``).
Deterministic: same seed → identical files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BASE_DIR
from app.synthetic.generator import SyntheticConfig, generate, strip_labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--customers", type=int, default=150)
    parser.add_argument("--days", type=int, default=6)
    parser.add_argument("--out", type=Path, default=BASE_DIR / "data" / "demo")
    args = parser.parse_args(argv)

    data = generate(
        SyntheticConfig(
            seed=args.seed,
            customers=args.customers,
            days=args.days,
            ato_attacks=0,
            app_scams=0,
            mule_rings=0,
            card_testing=0,
            structuring=0,
            sanctions_hits=0,
        )
    )
    args.out.mkdir(parents=True, exist_ok=True)
    customer_ibans = {c["iban"] for c in data.customers}
    used = {t["beneficiary_iban"] for t in data.transactions if t.get("beneficiary_iban")}
    payees = [p for p in data.payees if p["iban"] in used or p["iban"] in customer_ibans]
    txs = [strip_labels(t) for t in data.transactions]
    for name, payload in (
        ("customers.json", data.customers),
        ("payees.json", payees),
        ("transactions.json", txs),
    ):
        (args.out / name).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    print(f"OK müşteri={len(data.customers)} alıcı={len(payees)} işlem={len(txs)} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
