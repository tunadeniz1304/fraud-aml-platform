"""Observability (P0.9): Prometheus metrics, readiness, OpenTelemetry shim."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.monitoring import tracing
from app.security.auth import Principal, issue_token

_TOKEN, _ = issue_token(Principal("analist", "analist", "analist"))
ANALYST = {"Authorization": f"Bearer {_TOKEN}"}
RISKY = {
    "transaction_id": "TX-OBS-1",
    "ts": "2026-09-22T03:10:00",
    "customer_id": "CUST-0004",
    "amount": 60000,
    "currency": "EUR",
    "device_id": "DEV-NEW-9",
    "location": "Berlin",
    "country": "DE",
    "ip_address": "45.84.2.2",
}


@pytest.fixture()
def client(app_env):
    with TestClient(create_app()) as c:
        yield c


class TestObservability:
    def test_metrics_and_readiness(self, client):
        assert client.post("/api/transactions", json=RISKY, headers=ANALYST).status_code == 200
        client.get("/api/cases/stats", headers=ANALYST)
        text = client.get("/metrics").text
        for name in (
            "fraud_transactions_total",
            "fraud_scoring_latency_seconds_bucket",
            "fraud_decisions_total",
            "fraud_alerts_total",
            "fraud_cases_open",
            "fraud_http_requests_total",
            "fraud_model_score_bucket",
        ):
            assert name in text, name
        assert 'route="/api/cases/stats"' in text
        ready = client.get("/api/health/ready").json()
        assert ready["status"] == "ready"
        assert ready["info"]["model"] == "fraud_gbm_v1" and ready["info"]["model_loaded"] is True
        assert client.get("/api/health/live").json() == {"status": "live"}


class TestTracing:
    def test_noop_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        assert tracing.setup_tracing() is False and not tracing.enabled()
        with tracing.span("x", a=1) as current:
            assert current is None

    def test_spans_with_a_tracer(self, monkeypatch):
        recorded: list[tuple[str, dict]] = []

        class FakeSpan:
            def __init__(self, name):
                self.name, self.attrs = name, {}

            def set_attribute(self, key, value):
                self.attrs[key] = value

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                recorded.append((self.name, self.attrs))

        class FakeTracer:
            def start_as_current_span(self, name):
                return FakeSpan(name)

        monkeypatch.setattr(tracing, "_tracer", FakeTracer())
        try:
            with tracing.span("scoring.score", transaction_id="T1", n=3, skip=None):
                pass
        finally:
            tracing.reset()
        assert recorded == [("scoring.score", {"transaction_id": "T1", "n": 3})]

    def test_endpoint_with_sdk_configures_exporter(self, monkeypatch):
        pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
        try:
            assert tracing.setup_tracing("test") is True and tracing.enabled()
        finally:
            tracing.reset()
