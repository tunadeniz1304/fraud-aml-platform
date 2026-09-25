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
# Unit tests use the small hand-written population (CUST-0001..5); the app
# itself defaults to data/demo.
_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.environ["FRAUD_CUSTOMERS_PATH"] = os.path.join(_DATA, "customers.json")
os.environ["FRAUD_TRANSACTIONS_PATH"] = os.path.join(_DATA, "transactions.json")
os.environ["FRAUD_PAYEES_PATH"] = os.path.join(_DATA, "payees.json")  # absent: no payees
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

from collections.abc import AsyncIterator, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

from app.core.event_bus import EventBus  # noqa: E402
from app.db import repository as repo  # noqa: E402
from app.db.database import Database  # noqa: E402
from app.db.models import AuditLog  # noqa: E402
from app.db.writer import PersistenceWriter  # noqa: E402
from app.features.extractor import CustomerDirectory  # noqa: E402
from app.services.accounts import AccountService  # noqa: E402

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


class PlatformStore:
    """Test facade over the async persistence stack (DB + writer + accounts)."""

    def __init__(self, db: Database, writer: PersistenceWriter, accounts: AccountService):
        self.db = db
        self.writer = writer
        self.accounts = accounts

    def get_status(self, customer_id: str) -> str | None:
        return self.accounts.get_status(customer_id)

    async def get_account(self, customer_id: str) -> dict[str, Any] | None:
        await self.writer.flush()
        return await self.accounts.get_account(customer_id)

    async def db_status(self, customer_id: str) -> str | None:
        account = await self.get_account(customer_id)
        return account["hesap_durumu"] if account else None

    async def set_status(self, customer_id: str, status: str) -> None:
        await self.accounts.set_by_analyst(customer_id, status, actor="test", reason="test")

    async def audit(self, event_type: str | None = None) -> list[dict[str, Any]]:
        await self.writer.flush()
        async with self.db.session() as session:
            rows = await repo.list_audit(session, limit=100_000)
        return [r for r in rows if event_type is None or r["event_type"] == event_type]

    async def count(self, model: Any) -> int:
        await self.writer.flush()
        async with self.db.session() as session:
            return await repo.count(session, model)


async def make_store(path: Path, customers: Path | None = None) -> PlatformStore:
    from app.config import get_settings

    db = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await db.create_all()
    directory = CustomerDirectory.from_file(customers or get_settings().resolved_customers_path)
    async with db.transaction() as session:
        await repo.upsert_customers(session, directory.records())
    writer = PersistenceWriter(db)
    accounts = AccountService(db, writer)
    await accounts.load()
    return PlatformStore(db, writer, accounts)


@pytest.fixture()
async def store(db_path: Path) -> AsyncIterator[PlatformStore]:
    platform = await make_store(db_path)
    yield platform
    await platform.writer.stop()
    await platform.db.dispose()


__all__ = ["AuditLog", "PlatformStore", "make_store"]


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
