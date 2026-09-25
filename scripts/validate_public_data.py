"""Run the public-data validation and write artifacts + plots (docs/VALIDATION_REPORT.md).

    python scripts/validate_public_data.py --dataset paysim --sample-frac 0.1
    python scripts/validate_public_data.py --dataset paysim --source fixture
    python scripts/validate_public_data.py --dataset elliptic
    python scripts/validate_public_data.py --dataset ulb
    python scripts/validate_public_data.py --dataset synthetic

Outputs ``artifacts/validation/<set>/metrics.json`` (committed, small) and
PR curves under ``docs/img/validation/``. Full data comes from
``data/external/`` (``scripts/fetch_public_fraud_data.py``); ``--source
fixture`` uses the small samples in ``tests/fixtures/`` (offline, fast).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from app.datasets import paysim_adapter as ps
from app.scoring.rules import load_ruleset
from app.validation import elliptic, ulb
from app.validation.evaluate import evaluate_replay, layer_scores, time_split
from app.validation.replay import ReplayResult, build_replay_engine, replay

ARTIFACTS = ROOT / "artifacts" / "validation"
IMG = ROOT / "docs" / "img" / "validation"
EXTERNAL = ROOT / "data" / "external"
FIXTURES = ROOT / "tests" / "fixtures"


def _write(name: str, payload: dict[str, Any]) -> Path:
    out = ARTIFACTS / name / "metrics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), **payload}
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"-> {out.relative_to(ROOT)}")
    return out


def _pr_plot(name: str, y: np.ndarray, scores: dict[str, np.ndarray], title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from sklearn.metrics import average_precision_score, precision_recall_curve

    IMG.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.4, 4.4), dpi=110)
    for layer, s in scores.items():
        precision, recall, _ = precision_recall_curve(y, s)
        ax.plot(recall, precision, label=f"{layer} (AP {average_precision_score(y, s):.3f})")
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(IMG / f"{name}_pr.png")
    plt.close(fig)


CACHE = EXTERNAL / "cache"


async def _replay_block(
    transactions: list[dict[str, Any]],
    customers: list[dict[str, Any]],
    *,
    paysim: bool,
    cache: Path | None = None,
) -> tuple[dict[str, Any], Any]:
    if cache is not None and cache.exists():  # noqa: ASYNC240 - one-off CLI check
        print(f"Önbellekteki replay kullanılıyor: {cache.name}")
        res = ReplayResult.load(cache)
    else:
        engine = build_replay_engine(customers, load_ruleset(), accounts_are_customer_ids=paysim)
        progress = 50_000 if len(transactions) > 100_000 else 0
        res = await replay(
            transactions, engine, expected=len(transactions), progress_every=progress
        )
        if cache is not None:
            res.save(cache)
    return evaluate_replay(res), res


def run_paysim(args: argparse.Namespace) -> None:
    flt = ps.PaySimFilter(
        sample_frac=args.sample_frac, step_min=args.step_min, step_max=args.step_max, seed=args.seed
    )
    if args.source == "fixture":
        path, name = FIXTURES / "paysim_sample.csv", "paysim_fixture"
    else:
        [path] = list((EXTERNAL / "paysim").glob("*.csv"))
        name = "paysim"
    started = time.perf_counter()
    txs = ps.load_transactions(path, flt)
    steps = [t["step"] for t in txs]
    cut = min(steps) + 0.70 * (max(steps) - min(steps))
    prior = statistics.median(t["amount_try"] for t in txs if t["step"] <= cut)
    customers = ps.customer_records(txs, prior_amount_try=round(prior, 2))
    elapsed = time.perf_counter() - started
    print(f"PaySim: {len(txs):,} olay, {len(customers):,} müşteri (okuma {elapsed:.0f} sn)")
    cache = None
    if args.reuse_replay:
        cache = CACHE / f"{name}_{args.sample_frac:g}_{args.step_min}_{args.step_max}.npz"
    block, res = asyncio.run(_replay_block(txs, customers, paysim=True, cache=cache))
    split = time_split(res)
    scores, _ = layer_scores(split, seed=args.seed)
    _pr_plot(name, split.test.label, scores, f"PaySim ({args.source}) — test dönemi PR eğrileri")
    _write(
        name,
        {
            "dataset": "PaySim",
            "source": str(path.relative_to(ROOT)),
            "subset": {
                "sample_frac": args.sample_frac,
                "sampling": "alıcı (nameDest) bazlı hash örneklemesi",
                "steps": [min(steps), max(steps)],
                "train_until_step": round(cut, 1),
            },
            "mapping": {
                "type_map": {k: list(v) for k, v in ps.TYPE_MAP.items()},
                "excluded_columns": list(ps.EXCLUDED_COLUMNS),
                "amount_prior_try": round(prior, 2),
            },
            **block,
        },
    )


def run_synthetic(args: argparse.Namespace) -> None:
    from app.synthetic.generator import SyntheticConfig, generate

    data = generate(SyntheticConfig(seed=args.seed))
    cache = CACHE / f"synthetic_{args.seed}.npz" if args.reuse_replay else None
    block, res = asyncio.run(
        _replay_block(data.transactions, data.customers, paysim=False, cache=cache)
    )
    split = time_split(res)
    scores, _ = layer_scores(split, seed=args.seed)
    _pr_plot("synthetic", split.test.label, scores, "Sentetik (seed 42) — test dönemi PR eğrileri")
    _write("synthetic", {"dataset": "Sentetik (arındırılmış üretici)", **data.summary(), **block})


def run_elliptic(args: argparse.Namespace) -> None:
    directory = FIXTURES / "elliptic_sample" if args.source == "fixture" else EXTERNAL / "elliptic"
    data = elliptic.load(directory)
    result = elliptic.evaluate(data, seed=args.seed)
    _write(
        "elliptic_fixture" if args.source == "fixture" else "elliptic",
        {"dataset": "Elliptic", "source": str(directory.relative_to(ROOT)), **result},
    )


def run_ulb(args: argparse.Namespace) -> None:
    path = EXTERNAL / "ulb" / "creditcard.csv"
    _write(
        "ulb", {"dataset": "ULB Credit Card (OpenML 1597)", **ulb.evaluate(path, seed=args.seed)}
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dataset", choices=["paysim", "elliptic", "ulb", "synthetic"], required=True
    )
    parser.add_argument("--source", choices=["full", "fixture"], default="full")
    parser.add_argument("--sample-frac", type=float, default=1.0)
    parser.add_argument("--step-min", type=int, default=1)
    parser.add_argument("--step-max", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--reuse-replay",
        action="store_true",
        help="replay'i data/external/cache/ altına kaydet / oradan yükle (kod değişince silin)",
    )
    args = parser.parse_args()
    {"paysim": run_paysim, "elliptic": run_elliptic, "ulb": run_ulb, "synthetic": run_synthetic}[
        args.dataset
    ](args)


if __name__ == "__main__":
    main()
