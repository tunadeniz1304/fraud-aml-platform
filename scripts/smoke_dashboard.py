"""Throwaway smoke test for the FastAPI dashboard (not committed as a test suite)."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import server
from app.api.dashboard import state


def main() -> None:
    asyncio.run(server.build_pipeline())
    client = TestClient(server.app)

    r = client.get("/api/health")
    assert r.status_code == 200, r.text
    print("health:", r.json())

    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    status_ = r.json()
    print(
        "status: monitored=%s analyzed=%s blocked=%s passed=%s vector=%s"
        % (
            status_["total_monitored"],
            status_["analyzed"],
            status_["blocked"],
            status_["passed"],
            status_["vector_customers"],
        )
    )
    assert status_["analyzed"] == 9
    assert status_["blocked"] == 3
    accounts = {a["customer_id"]: a["hesap_durumu"] for a in status_["accounts"]}
    assert accounts.get("CUST-0001") == "BLOKE", accounts
    print("accounts sample:", dict(list(accounts.items())[:3]))

    r = client.get("/api/transactions")
    assert r.status_code == 200
    assert len(r.json()) == 9

    r = client.get("/api/blocks")
    blocks = r.json()
    assert len(blocks) == 3

    r = client.get("/api/audit")
    audit = r.json()
    assert len(audit) >= 9

    # Live ingest: a clean transfer -> passes.
    payload = {
        "transaction_id": "TX-LIVE-1",
        "ts": "2026-09-22T15:00:00",
        "customer_id": "CUST-0001",
        "amount": 1500,
        "currency": "TRY",
        "device_id": "DEV-9F2A-11",
        "location": "İstanbul",
        "country": "TR",
        "purpose": "Shopping",
    }
    r = client.post("/api/transactions", json=payload)
    assert r.status_code == 200, r.text
    print("ingest:", r.json()["transaction_id"], "risk=", r.json()["risk_score"])

    # Invalid payload must be rejected by Pydantic strict validation.
    bad = dict(payload, amount=-5)
    r = client.post("/api/transactions", json=bad)
    assert r.status_code == 422, r.text
    print("bad ingest correctly rejected:", r.status_code)

    print("DASHBOARD SMOKE OK")


if __name__ == "__main__":
    main()
