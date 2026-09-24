"""Load test and latency report (P2.2).

    python scripts/load_test.py --mode engine --count 5000   # senkron skor yolu
    python scripts/load_test.py --mode http --url http://localhost:8010 --concurrency 64
    python scripts/load_test.py --mode both --url http://localhost:8010 --report docs/PERFORMANCE.md

``engine`` measures the synchronous scoring path (features → rules → LightGBM →
anomaly → graph/APP/online signals → policy) exactly as the pipeline runs it.
``http`` fires authenticated ``POST /api/transactions`` requests concurrently
(end-to-end: validation, bus, scoring, action, write-behind persistence).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("FRAUD_SKIP_DOTENV", "1")
os.environ.setdefault("LLM_MODE", "demo")


def percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def q(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))], 3)

    return {
        "p50": round(statistics.median(ordered), 3),
        "p95": q(0.95),
        "p99": q(0.99),
        "max": round(ordered[-1], 3),
    }


def synthetic(count: int, seed: int = 11) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from app.synthetic.generator import SyntheticConfig, generate, strip_labels

    days = max(12, count // 400 + 1)
    data = generate(SyntheticConfig(seed=seed, customers=150, days=days))
    return data.customers, [strip_labels(t) for t in data.transactions[:count]]


async def engine_benchmark(count: int) -> dict[str, Any]:
    from app.features.extractor import CustomerDirectory, FeatureExtractor
    from app.scoring.engine import ScoringEngine

    customers, txs = synthetic(count)
    engine = ScoringEngine.from_settings(FeatureExtractor(customers=CustomerDirectory(customers)))
    for tx in txs[:200]:  # warm-up (imports, caches, model pages)
        await engine.score({**tx, "transaction_id": f"W-{tx['transaction_id']}"})
    latencies = []
    started = time.perf_counter()
    for tx in txs:
        result = await engine.score(tx)
        await engine.commit(result)
        latencies.append(result.latency_ms)
    elapsed = time.perf_counter() - started
    return {
        "mode": "engine",
        "n": len(latencies),
        "tps": round(len(latencies) / elapsed, 1),
        "latency_ms": percentiles(latencies),
    }


async def http_load(url: str, count: int, concurrency: int, api_key: str | None) -> dict[str, Any]:
    import httpx

    customers, txs = synthetic(count, seed=23)
    headers: dict[str, str] = {}
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        if api_key:
            headers["X-API-Key"] = api_key
        else:
            r = await client.post(
                "/api/auth/login", json={"username": "analist", "password": "analist123"}
            )
            r.raise_for_status()
            headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        known = {
            c["customer_id"] for c in (await client.get("/api/accounts", headers=headers)).json()
        }
        if not known.intersection(c["customer_id"] for c in customers):
            print("Uyarı: sunucu farklı bir müşteri nüfusu kullanıyor (bilinmeyen müşteri).")
        run = uuid.uuid4().hex[:6]
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for tx in txs:
            queue.put_nowait({**tx, "transaction_id": f"LT-{run}-{tx['transaction_id']}"})
        latencies: list[float] = []
        errors = 0

        async def worker() -> None:
            nonlocal errors
            while not queue.empty():
                tx = queue.get_nowait()
                t0 = time.perf_counter()
                try:
                    resp = await client.post("/api/transactions", json=tx, headers=headers)
                    if resp.status_code != 200:
                        errors += 1
                except httpx.HTTPError:
                    errors += 1
                latencies.append((time.perf_counter() - t0) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed = time.perf_counter() - started
    return {
        "mode": "http",
        "n": len(latencies),
        "errors": errors,
        "concurrency": concurrency,
        "tps": round(len(latencies) / elapsed, 1),
        "latency_ms": percentiles(latencies),
    }


def to_markdown(results: list[dict[str, Any]]) -> str:
    rows = [
        f"| {r['mode']} | {r['n']} | {r.get('concurrency', 1)} | {r['tps']} | "
        f"{r['latency_ms']['p50']} | {r['latency_ms']['p95']} | {r['latency_ms']['p99']} | "
        f"{r['latency_ms']['max']} | {r.get('errors', 0)} |"
        for r in results
    ]
    return "\n".join(
        [
            f"### Ölçüm — {datetime.now():%Y-%m-%d %H:%M} · {platform.system()} · Python "
            f"{platform.python_version()} · {os.cpu_count()} CPU",
            "",
            "| Mod | İstek | Eşzamanlılık | TPS | p50 ms | p95 ms | p99 ms | max ms | Hata |",
            "|---|---|---|---|---|---|---|---|---|",
            *rows,
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["engine", "http", "both"], default="engine")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=3000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--api-key", default=os.environ.get("SERVICE_API_KEY"))
    parser.add_argument("--report", type=Path, default=None, help="Markdown raporuna ekle")
    args = parser.parse_args(argv)

    results = []
    if args.mode in ("engine", "both"):
        results.append(asyncio.run(engine_benchmark(args.count)))
    if args.mode in ("http", "both"):
        results.append(asyncio.run(http_load(args.url, args.count, args.concurrency, args.api_key)))
    table = to_markdown(results)
    print(table)
    if args.report:
        with args.report.open("a", encoding="utf-8") as fh:
            fh.write("\n" + table)
    engine = next((r for r in results if r["mode"] == "engine"), None)
    return 0 if engine is None or engine["latency_ms"]["p99"] < 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())
