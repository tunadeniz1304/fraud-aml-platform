"""Tests for API hardening: security headers and optional admin token gate."""

from __future__ import annotations

import os
from unittest import mock

from fastapi.testclient import TestClient

from app.api import dashboard


def _client():
    return TestClient(dashboard.app)


class TestSecurityHeaders:
    def test_security_headers_present(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            c = _client()
            r = c.get("/api/health")
            assert r.status_code == 200
            assert r.headers.get("x-content-type-options") == "nosniff"
            assert r.headers.get("x-frame-options") == "DENY"
            assert "default-src 'self'" in r.headers.get("content-security-policy", "")

    def test_csp_applies_to_dashboard_page(self):
        with mock.patch.dict(os.environ, {"ADMIN_TOKEN": "s3cret"}, clear=False):
            c = _client()
            r = c.get("/")
            assert r.status_code == 200
            assert "content-security-policy" in r.headers


class TestAdminTokenGate:
    def test_admin_requires_token_when_configured(self):
        with mock.patch.dict(os.environ, {"ADMIN_TOKEN": "s3cret"}, clear=False):
            c = _client()
            r = c.get("/api/admin/accounts")
            assert r.status_code == 401
            r = c.get(
                "/api/admin/accounts",
                headers={"Authorization": "Bearer s3cret"},
            )
            assert r.status_code == 200

    def test_open_endpoints_ungated(self):
        """Health stays public; data endpoints now require auth (RBAC, P0.8)."""
        with mock.patch.dict(os.environ, {"ADMIN_TOKEN": "s3cret"}, clear=False):
            c = _client()
            assert c.get("/api/health").status_code == 200
            assert c.get("/api/status").status_code == 401  # no credentials
            authed = c.get("/api/status", headers={"Authorization": "Bearer s3cret"})
            assert authed.status_code == 503  # pipeline not wired
