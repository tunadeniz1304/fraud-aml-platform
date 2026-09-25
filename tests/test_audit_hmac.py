"""Keyed audit chain (audit M9): HMAC hashes and verify-failure alerting."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.config import get_settings
from app.db.audit import GENESIS, AuditEntry, canonical, chain_hash, verify_chain
from app.monitoring.metrics import AUDIT_VERIFY_FAILURES

ENTRY = AuditEntry(event_type="test", reason="hmac", entity_id="E1")


@pytest.fixture()
def keyed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-audit-key-0123456789")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_unkeyed_chain_is_plain_sha256():
    expected = hashlib.sha256((GENESIS + canonical(ENTRY)).encode()).hexdigest()
    assert chain_hash(GENESIS, ENTRY) == expected


def test_keyed_chain_uses_hmac(keyed):
    message = (GENESIS + canonical(ENTRY)).encode()
    key = b"test-audit-key-0123456789"
    assert chain_hash(GENESIS, ENTRY) == hmac.new(key, message, hashlib.sha256).hexdigest()
    assert chain_hash(GENESIS, ENTRY) != hashlib.sha256(message).hexdigest()


async def test_chain_rebuilt_without_the_key_fails_and_is_counted(
    store, monkeypatch: pytest.MonkeyPatch
):
    # Rows written without the key (as an attacker without it would) do not
    # verify once the key is configured, and the failure is exported as a metric.
    for entry in (ENTRY, AuditEntry(event_type="test", reason="2")):
        await store.writer.record_audit(entry)
    await store.writer.flush()
    async with store.db.session() as session:
        assert (await verify_chain(session)).ok

    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-audit-key-0123456789")
    get_settings.cache_clear()
    try:
        before = AUDIT_VERIFY_FAILURES._value.get()
        async with store.db.session() as session:
            result = await verify_chain(session)
        assert not result.ok and result.checked == 0
        assert AUDIT_VERIFY_FAILURES._value.get() == before + 1
    finally:
        get_settings.cache_clear()
