"""F7: live SSE feed + summary, SPA serving, consortium deny-list, FedAvg, load-test report."""

from __future__ import annotations

import json

import numpy as np
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.config import get_settings
from app.consortium import ConsortiumHub, digest, federated_average, publish_fraud
from app.security.auth import Principal, issue_token

TOKEN, _ = issue_token(Principal("analist", "analist", "analist"))
H = {"Authorization": f"Bearer {TOKEN}"}
TX = {
    "transaction_id": "TX-LIVE-1",
    "ts": "2026-09-22T14:00:00",
    "customer_id": "CUST-0003",
    "amount": 700,
    "currency": "TRY",
    "device_id": "DEV-88CC-11",
    "location": "İzmir",
    "country": "TR",
}


class TestConsortium:
    def test_hashing_and_cross_bank_hits(self):
        hub = ConsortiumHub()
        key = hub.report("BANKA_B", "iban", "TR33 0006 1005 1978 6457 8413 26")
        assert key == digest("iban", "TR330006100519786457841326") and len(key) == 64
        assert "TR33" not in json.dumps(sorted(hub.entries))  # never stored in clear text
        assert hub.hits("iban", "tr330006100519786457841326") == {"BANKA_B"}
        publish_fraud(hub, ["TR11"], ["DEV-X", ""])
        assert hub.hits("iban", "TR11") == set()  # own reports do not self-trigger
        assert hub.hits("device", "") == set()
        assert hub.stats()["by_bank"] == {"ANIL3": 2, "BANKA_B": 1}
        assert digest("iban", "TR11", salt="a") != digest("iban", "TR11", salt="b")

    def test_federated_average_learns_shared_pattern(self):
        rng = np.random.default_rng(0)
        banks = []
        for _ in range(3):
            x = rng.normal(size=(400, 2))
            y = (x[:, 0] + 0.5 * x[:, 1] > 0.3).astype(float)
            banks.append((x, y))
        w = federated_average(banks)
        x_all = np.vstack([b[0] for b in banks])
        y_all = np.concatenate([b[1] for b in banks])
        pred = (np.hstack([x_all, np.ones((len(x_all), 1))]) @ w) > 0
        assert (pred == y_all).mean() > 0.9


class TestLiveAndSpa:
    def test_live_summary_stream_and_consortium_api(self, app_env):
        with TestClient(create_app()) as client:
            assert client.post("/api/transactions", json=TX, headers=H).status_code == 200
            summary = client.get("/api/live/summary", headers=H).json()
            assert summary["window"] >= 1 and summary["latency_ms"]["p99"] is not None
            assert client.get("/api/live/summary").status_code == 401
            pipeline = client.app.state  # noqa: F841 - app is running
            from app.api.state import state

            queue = state.pipeline.subscribe_live()
            client.post("/api/transactions", json={**TX, "transaction_id": "TX-LIVE-2"}, headers=H)
            assert queue.get_nowait()["transaction_id"] == "TX-LIVE-2"
            state.pipeline.unsubscribe_live(queue)

            stats = client.get("/api/consortium/stats", headers=H).json()
            assert "entries" in stats
            report = client.post(
                "/api/consortium/report",
                json={"bank": "BANKA_C", "kind": "iban", "value": "TR990006100519786457841399"},
                headers=H,
            )
            assert report.status_code == 403  # simulated partner feed needs admin
            admin, _ = issue_token(Principal("admin", "admin", "admin"))
            ok = client.post(
                "/api/consortium/report",
                json={"bank": "BANKA_C", "kind": "iban", "value": "TR990006100519786457841399"},
                headers={"Authorization": f"Bearer {admin}"},
            )
            assert ok.status_code == 200 and len(ok.json()["digest"]) == 64
            hit = client.post(
                "/api/transactions",
                json={
                    **TX,
                    "transaction_id": "TX-LIVE-3",
                    "beneficiary_iban": "TR990006100519786457841399",
                },
                headers=H,
            ).json()
            codes = {r["code"] for r in hit["reason_codes"]}
            assert "CONSORTIUM_HIT" in codes or "R_CONSORTIUM_HIT" in codes
            assert hit["decision"] in ("HOLD", "BLOCK")

    def test_sse_endpoint_requires_role(self, app_env):
        with TestClient(create_app()) as client:
            assert client.get("/api/live/stream").status_code == 401

    def test_spa_served_when_built(self, app_env, tmp_path, monkeypatch):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        monkeypatch.setenv("FRONTEND_DIST", str(dist))
        get_settings.cache_clear()
        with TestClient(create_app()) as client:
            root = client.get("/")
            assert (
                'id="root"' in root.text
                and "unsafe-inline" not in root.headers["content-security-policy"]
            )
            assert client.get("/app/assets/app.js").status_code == 200
            legacy = client.get("/legacy")
            assert legacy.status_code == 200 and "api-base" in legacy.text


def test_load_test_engine_mode_reports_p99(tmp_path):
    import scripts.load_test as lt

    report = tmp_path / "PERF.md"
    # The 50 ms SLO gate depends on host load; this test covers report generation.
    argv = ["--mode", "engine", "--count", "400", "--report", str(report)]
    assert lt.main([*argv, "--p99-budget-ms", "10000"]) == 0
    text = report.read_text(encoding="utf-8")
    assert "| engine | 400 |" in text and "p99" in text


def test_load_test_engine_mode_fails_over_p99_budget():
    import scripts.load_test as lt

    argv = ["--mode", "engine", "--count", "50", "--p99-budget-ms", "0"]
    assert lt.main(argv) == 1
