"""Pytest configuration: isolated SQLite per test + bit of event bus."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.account_store import AccountStore
from app.core.event_bus import EventBus

pytest_plugins = ["pytest_asyncio"]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_fraud.db"


@pytest.fixture()
def store(db_path: Path) -> AccountStore:
    account_store = AccountStore(db_path=db_path)
    account_store.seed_accounts()
    return account_store


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def sample_transactions() -> list[dict]:
    """Deterministic minimal stream: clean + clearly anomalous transfers."""
    return [
        {
            "transaction_id": "TX-A1",
            "ts": "2026-09-22T14:30:00",
            "customer_id": "CUST-0001",
            "amount": 2000,
            "currency": "TRY",
            "device_id": "DEV-9F2A-11",
            "location": "İstanbul",
            "country": "TR",
            "purpose": "Salary payment",
        },
        {
            "transaction_id": "TX-A2",
            "ts": "2026-09-22T14:31:00",
            "customer_id": "CUST-0001",
            "amount": 2600,
            "currency": "TRY",
            "device_id": "DEV-3C81-55",
            "location": "İstanbul",
            "country": "TR",
            "purpose": "Peer transfer",
        },
        {
            "transaction_id": "TX-B1",
            "ts": "2026-09-22T02:15:00",
            "customer_id": "CUST-0002",
            "amount": 90000,
            "currency": "EUR",
            "device_id": "DEV-33CC-10",
            "location": "Berlin",
            "country": "DE",
            "purpose": "Investment",
        },
        {
            "transaction_id": "TX-C1",
            "ts": "2026-09-22T03:45:00",
            "customer_id": "CUST-0003",
            "amount": 50000,
            "currency": "TRY",
            "device_id": "DEV-FF99-12",
            "location": "Dubai",
            "country": "AE",
            "purpose": "Gift",
        },
    ]
