"""Build the small, offline test fixtures from the downloaded public datasets.

    python scripts/fetch_public_fraud_data.py --only paysim --only elliptic
    python scripts/build_fixtures.py

* ``tests/fixtures/paysim_sample.csv`` — stratified by (``type``, ``isFraud``)
  so the fraud rate and the transaction-type mix of the full file are kept;
  at most 20 000 rows.
* ``tests/fixtures/elliptic_sample/`` — breadth-first connected subgraphs
  around illicit nodes of two training time steps and one test time step
  (Elliptic edges never cross time steps); rows copied verbatim (the
  CC BY-NC-ND 4.0 licence forbids sharing adapted material).

Each sample carries an ``ATTRIBUTION.md`` with source, licence and citation.
Deterministic (fixed seed).
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXTERNAL = ROOT / "data" / "external"
FIXTURES = ROOT / "tests" / "fixtures"
SEED = 20260925
PAYSIM_ROWS = 20_000
ELLIPTIC_STEPS = (10, 30, 42)
ELLIPTIC_NODES_PER_STEP = 170


def _manifest() -> dict:
    return json.loads((EXTERNAL / "manifest.json").read_text(encoding="utf-8"))


def _attribution(target: Path, key: str, note: str) -> None:
    meta = _manifest()[key]
    shas = "\n".join(f"- `{f['file']}` — SHA-256 `{f['sha256']}`" for f in meta["files"])
    target.write_text(
        f"# {meta['title']} — test örneği\n\n"
        f"- Kaynak: {meta['source']}\n- Lisans: {meta['license']}\n"
        f"- Atıf: {meta['citation']}\n\n{note}\n\nKaynak dosyalar:\n{shas}\n",
        encoding="utf-8",
    )


def build_paysim() -> Path:
    [src] = list((EXTERNAL / "paysim").glob("*.csv"))
    df = pd.read_csv(src)
    frac = PAYSIM_ROWS / len(df)
    parts = []
    rng = np.random.default_rng(SEED)
    for _, group in df.groupby(["type", "isFraud"], sort=True):
        k = max(1, round(len(group) * frac))
        idx = rng.choice(len(group), size=min(k, len(group)), replace=False)
        parts.append(group.iloc[np.sort(idx)])
    sample = pd.concat(parts).sort_index()
    if len(sample) > PAYSIM_ROWS:  # rounding: drop surplus legit rows only
        legit = sample.index[sample["isFraud"] == 0]
        drop = rng.choice(legit, size=len(sample) - PAYSIM_ROWS, replace=False)
        sample = sample.drop(index=drop)
    out = FIXTURES / "paysim_sample.csv"
    FIXTURES.mkdir(parents=True, exist_ok=True)
    sample.to_csv(out, index=False)
    rate_full = df["isFraud"].mean()
    rate_sample = sample["isFraud"].mean()
    _attribution(
        FIXTURES / "paysim_sample.ATTRIBUTION.md",
        "paysim",
        f"`paysim_sample.csv`: {len(sample)} satır, {int(sample['isFraud'].sum())} fraud "
        f"(oran %{rate_sample * 100:.3f}; tam veri %{rate_full * 100:.3f}); "
        f"(`type`, `isFraud`) katmanlı rastgele örnek, seed {SEED}.",
    )
    print(f"PaySim örneği: {len(sample)} satır, fraud {rate_sample:.5f} (tam {rate_full:.5f})")
    return out


def _bfs(adj: dict[int, set[int]], start: int, limit: int) -> list[int]:
    seen, queue, order = {start}, deque([start]), [start]
    while queue and len(order) < limit:
        node = queue.popleft()
        for nxt in sorted(adj.get(node, ())):
            if nxt not in seen:
                seen.add(nxt)
                order.append(nxt)
                queue.append(nxt)
                if len(order) >= limit:
                    break
    return order


def build_elliptic() -> Path:
    base = EXTERNAL / "elliptic"
    classes = pd.read_csv(base / "elliptic_txs_classes.csv")
    edges = pd.read_csv(base / "elliptic_txs_edgelist.csv")
    features = pd.read_csv(base / "elliptic_txs_features.csv", header=None)
    step_of = dict(zip(features[0], features[1], strict=True))
    label = dict(zip(classes["txId"], classes["class"].astype(str), strict=True))
    adj: dict[int, set[int]] = {}
    for a, b in zip(edges["txId1"], edges["txId2"], strict=True):
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    chosen: list[int] = []
    for step in ELLIPTIC_STEPS:
        illicit = sorted(
            n for n, s in step_of.items() if s == step and label.get(n) == "1" and n in adj
        )
        # the illicit node with the largest neighbourhood seeds the component
        seed = max(illicit, key=lambda n: (len(adj[n]), -n))
        chosen += _bfs(adj, seed, ELLIPTIC_NODES_PER_STEP)
    keep = set(chosen)
    out = FIXTURES / "elliptic_sample"
    out.mkdir(parents=True, exist_ok=True)
    # verbatim rows: the Elliptic licence (CC BY-NC-ND 4.0) allows sharing a
    # part of the data unmodified, not adapted (no rounding / re-encoding)
    raw = (base / "elliptic_txs_features.csv").read_text(encoding="utf-8").splitlines()
    kept = [line for line in raw if int(line.split(",", 1)[0]) in keep]
    with (out / "elliptic_txs_features.csv").open("w", encoding="utf-8", newline="") as fh:
        fh.writelines(line + "\n" for line in kept)
    classes[classes["txId"].isin(keep)].to_csv(out / "elliptic_txs_classes.csv", index=False)
    edges[edges["txId1"].isin(keep) & edges["txId2"].isin(keep)].to_csv(
        out / "elliptic_txs_edgelist.csv", index=False
    )
    counts = classes[classes["txId"].isin(keep)]["class"].astype(str).value_counts().to_dict()
    _attribution(
        out / "ATTRIBUTION.md",
        "elliptic",
        f"Zaman adımları {ELLIPTIC_STEPS} içinden, en geniş komşuluğa sahip yasa dışı "
        f"düğümden başlayan genişlik öncelikli bağlı alt graflar (adım başına en çok "
        f"{ELLIPTIC_NODES_PER_STEP} düğüm); sınıf dağılımı {counts}. Satırlar **değiştirilmeden** "
        "kopyalandı (CC BY-NC-ND 4.0: yalnız ticari olmayan kullanım, türev paylaşımı yok).",
    )
    print(f"Elliptic örneği: {len(keep)} düğüm, sınıflar {counts}")
    return out


if __name__ == "__main__":
    build_paysim()
    build_elliptic()
