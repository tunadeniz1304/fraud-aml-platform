"""Pytest configuration.

The environment is pinned **before** any ``app`` import: ``.env`` files are
never loaded, the LLM runs in deterministic demo mode, no network is used and
every test gets an isolated SQLite database.
"""

from __future__ import annotations

import os

os.environ["FRAUD_SKIP_DOTENV"] = "1"
os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_MODE"] = "demo"
os.environ["VECTOR_STORE"] = "off"
os.environ["STREAM_MODE"] = "off"
for _name in (
    "LLM_API_KEY",
    "DEEPSEEK_API_KEY",
    "EVREN_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ADMIN_TOKEN",
    "SERVICE_API_KEY",
    "SERVICE_HMAC_SECRET",
):
    os.environ.pop(_name, None)

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from app.core.account_store import AccountStore  # noqa: E402
from app.core.event_bus import EventBus  # noqa: E402

pytest_plugins = ["pytest_asyncio"]


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> Iterator[None]:
    from app.security.ratelimit import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture()
def app_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Isolated DB + fresh settings for FastAPI lifespan tests."""
    from app.config import get_settings

    monkeypatch.setenv("FRAUD_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("STREAM_MODE", "batch")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_fraud.db"


@pytest.fixture()
def store(db_path: Path) -> Iterator[AccountStore]:
    account_store = AccountStore(db_path=db_path)
    account_store.seed_accounts()
    yield account_store
    account_store.close()


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


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    from app.security.auth import Principal, issue_token

    token, _ = issue_token(Principal("admin", "admin", "Test Admin"))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def analyst_headers() -> dict[str, str]:
    from app.security.auth import Principal, issue_token

    token, _ = issue_token(Principal("analist", "analist", "Test Analist"))
    return {"Authorization": f"Bearer {token}"}
