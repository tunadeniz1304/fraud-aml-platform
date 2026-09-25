"""P0.8 security tests: JWT login, RBAC, service credentials, rate limits,
request-id correlation and sanitised errors."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.security.auth import (
    Principal,
    UserDirectory,
    hash_password,
    issue_token,
    sign_body,
    verify_password,
    verify_signature,
)

TX = {
    "transaction_id": "TX-SVC-1",
    "ts": "2026-09-22T15:00:00",
    "customer_id": "CUST-0001",
    "amount": 1500,
    "currency": "TRY",
    "device_id": "DEV-9F2A-11",
    "location": "İstanbul",
    "country": "TR",
    "purpose": "Market",
}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


class TestPasswords:
    def test_hash_roundtrip_and_salt(self):
        h1, h2 = hash_password("parola"), hash_password("parola")
        assert h1 != h2 and verify_password("parola", h1) and not verify_password("x", h1)
        assert not verify_password("parola", "bozuk-kayit")


class TestLogin:
    @pytest.mark.parametrize(
        ("user", "pw", "role"),
        [
            ("analist", "analist123", "analist"),
            ("kidemli_analist", "kidemli123", "kidemli_analist"),
            ("admin", "admin123", "admin"),
        ],
    )
    def test_demo_users_can_log_in(self, client, user, pw, role):
        r = client.post("/api/auth/login", json={"username": user, "password": pw})
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == role and body["token_type"] == "bearer"
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.json()["username"] == user

    def test_wrong_password_401_without_detail_leak(self, client):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
        r2 = client.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
        assert r2.json() == r.json()  # no user-enumeration signal

    def test_login_rate_limited(self, client):
        codes = [
            client.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code
            for _ in range(12)
        ]
        assert codes[:10] == [401] * 10 and codes[-1] == 429

    def test_tampered_and_expired_tokens_rejected(self, client):
        token = login(client, "analist", "analist123")
        bad = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
        assert (
            client.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"}).status_code
            == 401
        )
        expired, _ = issue_token(Principal("analist", "analist"), ttl_minutes=-1)
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
        assert r.status_code == 401 and "süresi" in r.json()["detail"]


class TestRBAC:
    def test_role_hierarchy(self):
        assert Principal("a", "admin").has_role("analist")
        assert not Principal("a", "analist").has_role("kidemli_analist")
        assert not Principal("s", "service").has_role("analist")

    def test_analyst_forbidden_on_admin(self, client, analyst_headers, admin_headers):
        assert client.get("/api/admin/accounts", headers=analyst_headers).status_code == 403
        assert client.get("/api/admin/accounts", headers=admin_headers).status_code == 200

    def test_unauthenticated_data_access_401(self, client):
        for path in ("/api/status", "/api/transactions", "/api/audit", "/api/llm/status"):
            assert client.get(path).status_code == 401, path

    def test_user_directory_authenticate(self):
        users = UserDirectory.with_demo_users()
        assert users.authenticate("admin", "admin123").role == "admin"


class TestIngestCredentials:
    def test_api_key(self, app_env, monkeypatch):
        monkeypatch.setenv("SERVICE_API_KEY", "svc-key-0123456789")
        with TestClient(create_app()) as c:
            ok = c.post("/api/transactions", json=TX, headers={"X-API-Key": "svc-key-0123456789"})
            assert ok.status_code == 200, ok.text
            assert ok.json()["transaction_id"] == "TX-SVC-1"
            bad = c.post("/api/transactions", json=TX, headers={"X-API-Key": "wrong"})
            assert bad.status_code == 401

    def test_hmac_signature(self, app_env, monkeypatch):
        monkeypatch.setenv("SERVICE_HMAC_SECRET", "hmac-secret-0123456789")
        body = json.dumps({**TX, "transaction_id": "TX-HMAC"}).encode()
        ts = str(int(time.time()))
        sig = sign_body("hmac-secret-0123456789", ts, body, "nonce-0001")
        headers = {
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Nonce": "nonce-0001",
            "X-Signature": sig,
        }
        with TestClient(create_app()) as c:
            assert c.post("/api/transactions", content=body, headers=headers).status_code == 200
            # the identical signed request again: the nonce was already used (replay)
            replay = c.post("/api/transactions", content=body, headers=headers)
            assert replay.status_code == 401 and "nonce" in replay.json()["detail"]
            tampered = body.replace(b"1500", b"9500")
            fresh = {**headers, "X-Nonce": "nonce-0002"}
            r = c.post("/api/transactions", content=tampered, headers=fresh)
            assert r.status_code == 401

    def test_hmac_replay_window(self):
        body = b"{}"
        old = str(int(time.time()) - 3600)
        assert not verify_signature(
            "k" * 12, old, sign_body("k" * 12, old, body, "n1"), body, nonce="n1"
        )
        now = str(int(time.time()))
        signed = sign_body("k" * 12, now, body, "n1")
        assert verify_signature("k" * 12, now, signed, body, nonce="n1")
        assert not verify_signature("k" * 12, now, signed, body)  # nonce is mandatory
        assert not verify_signature("", old, "sha256=x", body)
        assert not verify_signature("k" * 12, "abc", "sha256=x", body)

    def test_analyst_jwt_can_ingest_and_rejections_are_422(self, app_env, analyst_headers):
        with TestClient(create_app()) as c:
            r = c.post("/api/transactions", json=TX, headers=analyst_headers)
            assert r.status_code == 200
            dup = c.post("/api/transactions", json=TX, headers=analyst_headers)
            assert dup.status_code == 200  # idempotent: same id -> same decision
            bad = c.post(
                "/api/transactions",
                json={**TX, "transaction_id": "TX-BAD-CCY", "currency": "XYZ"},
                headers=analyst_headers,
            )
            assert bad.status_code == 422
            assert "unsupported currency XYZ" in json.dumps(bad.json())


class TestHttpHardening:
    def test_request_id_generated_and_echoed(self, client):
        r = client.get("/api/health")
        assert len(r.headers["x-request-id"]) == 32
        r2 = client.get("/api/health", headers={"X-Request-ID": "abc-123"})
        assert r2.headers["x-request-id"] == "abc-123"
        r3 = client.get("/api/health", headers={"X-Request-ID": "bad id\n<script>"})
        assert r3.headers["x-request-id"] != "bad id\n<script>"

    def test_unhandled_error_is_sanitised(self):
        app = create_app()

        @app.get("/api/_boom")
        async def boom() -> None:
            raise RuntimeError("iç sır: db parolası")

        r = TestClient(app, raise_server_exceptions=False).get("/api/_boom")
        assert r.status_code == 500
        assert "sır" not in r.text and r.json()["request_id"]

    def test_security_headers_and_no_cors_by_default(self, client):
        r = client.get("/api/health", headers={"Origin": "https://evil.example"})
        assert r.headers["x-frame-options"] == "DENY"
        assert "access-control-allow-origin" not in r.headers

    def test_metrics_endpoint_requires_token(self, client, monkeypatch):
        from app.config import get_settings

        assert client.get("/metrics").status_code == 404  # no token configured
        monkeypatch.setenv("METRICS_TOKEN", "metrics-token-for-tests")
        get_settings.cache_clear()
        try:
            assert client.get("/metrics").status_code == 401
            ok = client.get("/metrics", headers={"Authorization": "Bearer metrics-token-for-tests"})
            assert ok.status_code == 200 and "fraud_llm_calls_total" in ok.text
        finally:
            get_settings.cache_clear()
