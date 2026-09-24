"""End-to-end smoke test of the HTTP surface.

In-process (default) it boots the app through the real FastAPI lifespan with a
throw-away SQLite database; with ``--url`` it targets a running deployment
(e.g. ``docker compose up``):

    python scripts/smoke_dashboard.py
    python scripts/smoke_dashboard.py --url http://localhost:8000
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def check(label: str, condition: bool, detail: Any = "") -> None:
    print(f"[{'OK' if condition else 'HATA'}] {label} {detail}")
    if not condition:
        raise SystemExit(1)


def run(client: Any) -> None:
    r = client.get("/api/health")
    check("health", r.status_code == 200, r.json())
    r = client.get("/api/status")
    check("kimliksiz erişim reddedildi", r.status_code == 401)
    r = client.post("/api/auth/login", json={"username": "analist", "password": "analist123"})
    check("giriş", r.status_code == 200)
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    status_ = client.get("/api/status", headers=headers).json()
    check("pipeline durumu", status_["analyzed"] >= 1, status_)
    txs = client.get("/api/transactions", headers=headers).json()
    check("işlem listesi", len(txs) >= 1, len(txs))
    llm = client.get("/api/llm/status", headers=headers).json()
    check("LLM durumu (anahtarsız)", "api_key" not in str(llm).lower(), llm)

    payload = {
        "transaction_id": "TX-SMOKE-1",
        "ts": "2026-09-22T15:00:00",
        "customer_id": "CUST-0001",
        "amount": 1500,
        "currency": "TRY",
        "device_id": "DEV-9F2A-11",
        "location": "İstanbul",
        "country": "TR",
        "purpose": "Market alışverişi",
    }
    r = client.post("/api/transactions", json=payload, headers=headers)
    check("canlı ingest", r.status_code == 200, r.json().get("risk_score"))
    r = client.post("/api/transactions", json={**payload, "amount": -5}, headers=headers)
    check("geçersiz ingest reddedildi", r.status_code == 422)
    r = client.post("/api/llm/explain/TX-SMOKE-1", headers=headers)
    check("LLM açıklaması", r.status_code == 200, r.json().get("llm_mode"))
    r = client.get("/")
    check("dashboard", r.status_code == 200 and "api-base" in r.text)
    check("CSP", "unsafe-inline" not in r.headers.get("content-security-policy", ""))
    print("DASHBOARD SMOKE OK")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Çalışan bir dağıtımın taban URL'i")
    args = parser.parse_args()
    if args.url:
        import httpx

        with httpx.Client(base_url=args.url, timeout=30) as client:
            run(client)
        return
    tmp = Path(tempfile.mkdtemp(prefix="anil3-smoke-"))
    os.environ.setdefault("FRAUD_DB_PATH", str(tmp / "smoke.db"))
    os.environ.setdefault("STREAM_MODE", "batch")
    os.environ.setdefault("VECTOR_STORE", "off")
    from fastapi.testclient import TestClient

    from app.api.dashboard import create_app

    with TestClient(create_app()) as client:
        run(client)


if __name__ == "__main__":
    main()
