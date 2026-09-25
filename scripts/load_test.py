"""Load test and latency report (P2.2, V6).

    python scripts/load_test.py --mode engine --count 5000          # motor (senkron skor yolu)
    python scripts/load_test.py --mode http-seq --url http://127.0.0.1:8010 --count 2000
    python scripts/load_test.py --mode http --url http://127.0.0.1:8010 \
        --concurrency 32 --tps 500 --seconds 20                     # eşzamanlı, hedef TPS
    python scripts/load_test.py --mode both --url ... --report docs/PERFORMANCE.md

``engine`` measures the synchronous scoring path (features → rules → LightGBM →
anomaly → graph/APP/online signals → policy) exactly as the pipeline runs it.

``http-seq`` is a **single client** sending authenticated ``POST
/api/transactions`` requests back to back (one in flight): pure per-request
latency, no queueing.

``http`` runs ``--concurrency`` clients. With ``--tps`` it is **open loop**:
request *i* is scheduled at ``start + i / tps`` whatever the server does, and
latency is measured from the *scheduled* time as well as from the send time,
so a server that falls behind cannot hide queueing (coordinated omission).
Without ``--tps`` the clients fire as fast as the server answers.

Traffic is the server's own demo population (``--source demo``: the warm-up
history replayed with fresh ids and current timestamps) so customers, devices
and payees are known; ``--source synthetic`` generates a fresh population.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("FRAUD_SKIP_DOTENV", "1")
os.environ.setdefault("LLM_MODE", "demo")

ROOT = Path(__file__).resolve().parent.parent
DEMO_TRANSACTIONS = ROOT / "data" / "demo" / "transactions.json"


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
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


def _now(utc: bool) -> str:
    """Send-time timestamp in the server's naive clock (a container runs in UTC)."""
    now = datetime.now(UTC).replace(tzinfo=None) if utc else datetime.now()
    return now.isoformat(timespec="milliseconds")


def demo_traffic(count: int, path: Path = DEMO_TRANSACTIONS) -> list[dict[str, Any]]:
    """The demo population's history, cycled to ``count`` events, as live traffic."""
    with path.open(encoding="utf-8") as fh:
        history = json.load(fh)
    run = uuid.uuid4().hex[:6]
    now = datetime.now().replace(microsecond=0).isoformat()
    return [
        {**history[i % len(history)], "transaction_id": f"LT-{run}-{i:06d}", "ts": now}
        for i in range(count)
    ]


def traffic(source: str, count: int, population: Path | None = None) -> list[dict[str, Any]]:
    if source == "demo":
        return demo_traffic(
            count, population / "transactions.json" if population else DEMO_TRANSACTIONS
        )
    run = uuid.uuid4().hex[:6]
    _, txs = synthetic(count, seed=23)
    return [{**tx, "transaction_id": f"LT-{run}-{tx['transaction_id']}"} for tx in txs]


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


async def _auth_headers(client: Any, api_key: str | None) -> dict[str, str]:
    if api_key:
        return {"X-API-Key": api_key}
    r = await client.post("/api/auth/login", json={"username": "analist", "password": "analist123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def http_load(
    url: str,
    count: int,
    concurrency: int,
    api_key: str | None,
    *,
    tps: float | None = None,
    source: str = "demo",
    population: Path | None = None,
    utc: bool = False,
    warmup: int = 50,
) -> dict[str, Any]:
    """``concurrency`` clients; open loop at ``tps`` when given (see module doc)."""
    import httpx

    txs = traffic(source, count + warmup, population)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=30, limits=limits) as client:
        headers = await _auth_headers(client, api_key)
        for tx in txs[:warmup]:  # connection setup, first-request caches
            await client.post("/api/transactions", json=tx, headers=headers)
        work = txs[warmup:]
        latencies: list[float] = []
        scheduled_latencies: list[float] = []
        errors = 0
        statuses: dict[int, int] = {}
        decisions: dict[str, int] = {}
        next_index = 0
        clock = time.perf_counter  # loop.time() ticks at ~15.6 ms on Windows
        started = clock()

        async def worker() -> None:
            nonlocal errors, next_index
            while next_index < len(work):
                index = next_index
                next_index += 1
                due = started + index / tps if tps else clock()
                delay = due - clock()
                if delay > 0:
                    await asyncio.sleep(delay)
                t0 = clock()
                try:
                    tx = {**work[index], "ts": _now(utc)}
                    resp = await client.post("/api/transactions", json=tx, headers=headers)
                    statuses[resp.status_code] = statuses.get(resp.status_code, 0) + 1
                    if resp.status_code != 200:
                        errors += 1
                    else:
                        decision = str(resp.json().get("decision"))
                        decisions[decision] = decisions.get(decision, 0) + 1
                except httpx.HTTPError:
                    errors += 1
                done = clock()
                latencies.append((done - t0) * 1000)
                scheduled_latencies.append((done - due) * 1000)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed = clock() - started
    mode = "http-seq" if concurrency == 1 and not tps else "http"
    result: dict[str, Any] = {
        "mode": mode,
        "n": len(latencies),
        "errors": errors,
        "statuses": statuses,
        "decisions": decisions,
        "concurrency": concurrency,
        "target_tps": tps,
        "tps": round(len(latencies) / elapsed, 1),
        "latency_ms": percentiles(latencies),
    }
    if tps:
        result["scheduled_latency_ms"] = percentiles(scheduled_latencies)
    return result


def to_markdown(results: list[dict[str, Any]], note: str = "") -> str:
    rows = []
    mixes = []
    for r in results:
        if r.get("decisions"):
            total = sum(r["decisions"].values())
            share = ", ".join(
                f"{k} %{100 * v / total:.0f}" for k, v in sorted(r["decisions"].items())
            )
            target = r.get("target_tps") or "-"
            mixes.append(f"- {r['mode']} @ {target} TPS karar dağılımı: {share}")
        lat = r["latency_ms"]
        sched = r.get("scheduled_latency_ms", {}).get("p99", "-")
        target = r.get("target_tps") or "-"
        rows.append(
            f"| {r['mode']} | {r['n']} | {r.get('concurrency', 1)} | {target} | {r['tps']} | "
            f"{lat['p50']} | {lat['p95']} | {lat['p99']} | {lat['max']} | {sched} | "
            f"{r.get('errors', 0)} |"
        )
    header = (
        f"### Ölçüm — {datetime.now():%Y-%m-%d %H:%M} · {platform.system()} · Python "
        f"{platform.python_version()} · {os.cpu_count()} mantıksal CPU"
    )
    return "\n".join(
        [
            header + (f" · {note}" if note else ""),
            "",
            "| Mod | İstek | Eşzamanlılık | Hedef TPS | TPS | p50 ms | p95 ms | p99 ms | max ms "
            "| p99 (planlanan) ms | Hata |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
            *rows,
            "",
            *mixes,
            *([""] if mixes else []),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    # Windows konsolu (cp1254) Türkçe yardım metninde UnicodeEncodeError verir.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["engine", "http", "http-seq", "both"], default="engine")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=3000)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--tps", type=float, default=None, help="open-loop hedef istek/sn")
    parser.add_argument("--seconds", type=float, default=None, help="--tps ile: süre (count=tps*s)")
    parser.add_argument("--source", choices=["demo", "synthetic"], default="demo")
    parser.add_argument(
        "--population",
        type=Path,
        default=None,
        help="demo nüfus dizini (build_demo_population.py --out); sunucu da aynısını yüklemeli",
    )
    parser.add_argument("--utc", action="store_true", help="ts'yi UTC gönder (konteyner saati)")
    parser.add_argument("--api-key", default=os.environ.get("SERVICE_API_KEY"))
    parser.add_argument("--note", default="", help="rapor başlığına eklenecek ortam notu")
    parser.add_argument("--json", type=Path, default=None, help="ham sonuçları JSON'a yaz")
    parser.add_argument("--report", type=Path, default=None, help="Markdown raporuna ekle")
    args = parser.parse_args(argv)
    count = int(args.tps * args.seconds) if args.tps and args.seconds else args.count

    results = []
    if args.mode in ("engine", "both"):
        results.append(asyncio.run(engine_benchmark(count)))
    if args.mode in ("http-seq", "both"):
        results.append(
            asyncio.run(
                http_load(
                    args.url,
                    count,
                    1,
                    args.api_key,
                    source=args.source,
                    population=args.population,
                    utc=args.utc,
                )
            )
        )
    if args.mode in ("http", "both"):
        results.append(
            asyncio.run(
                http_load(
                    args.url,
                    count,
                    args.concurrency,
                    args.api_key,
                    tps=args.tps,
                    source=args.source,
                    population=args.population,
                    utc=args.utc,
                )
            )
        )
    table = to_markdown(results, args.note)
    print(table)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    if args.report:
        with args.report.open("a", encoding="utf-8") as fh:
            fh.write("\n" + table)
    engine = next((r for r in results if r["mode"] == "engine"), None)
    return 0 if engine is None or engine["latency_ms"]["p99"] < 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())
