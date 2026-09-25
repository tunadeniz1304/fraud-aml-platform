"""Regression tests for the security audit findings (H1, H2, M7 …).

Each test names the finding it pins down.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.config import BASE_DIR, get_settings
from app.security.auth import Principal, issue_token

DEMO = BASE_DIR / "data" / "demo"
SVC = "svc-credential-for-tests-01"


def _bearer(user: str, role: str) -> dict[str, str]:
    token, _ = issue_token(Principal(user, role, user))  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def demo_env(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FRAUD_CUSTOMERS_PATH", str(DEMO / "customers.json"))
    monkeypatch.setenv("FRAUD_PAYEES_PATH", str(DEMO / "payees.json"))
    monkeypatch.setenv("FRAUD_TRANSACTIONS_PATH", str(DEMO / "transactions.json"))
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("RING_DETECT_INTERVAL_S", "0")
    monkeypatch.setenv("SERVICE_API_KEY", SVC)
    get_settings.cache_clear()
    return app_env


@pytest.fixture()
def prod_env(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("JWT_SECRET", "prod-signing-material-" + "x" * 24)
    monkeypatch.setenv("CONSORTIUM_SALT", "prod-consortium-" + "y" * 24)
    monkeypatch.setenv("SEED_DEMO_USERS", "false")
    get_settings.cache_clear()
    return demo_env


def _step_up_tx(tx_id: str) -> dict[str, Any]:
    """New device + new payee at 6x the usual amount -> STEP_UP (see test_audit_v2 A2)."""
    c = json.loads((DEMO / "customers.json").read_text(encoding="utf-8"))[5]
    avg = float(c.get("avg_amount") or 1000)
    return {
        "transaction_id": tx_id,
        "ts": datetime.now().replace(microsecond=0).isoformat(),
        "customer_id": c["customer_id"],
        "amount": avg * 6,
        "currency": "TRY",
        "device_id": "DEV-NEWPHONE-01",
        "location": (c.get("known_locations") or ["İstanbul"])[0],
        "country": "TR",
        "channel": (c.get("known_channels") or ["mobile"])[0],
        "beneficiary_id": "TR330006100519786457841326",
        "beneficiary_name": "Mehmet Kaya",
        "purpose": "kira",
    }


# --- H1: step-up result needs a one-time challenge ---------------------------------------
class TestStepUpChallenge:
    def test_h1_result_requires_consumable_challenge(self, demo_env: Path) -> None:
        svc = {"X-API-Key": SVC}
        with TestClient(create_app()) as c:
            first = c.post("/api/transactions", json=_step_up_tx("TX-SU-H1-01"), headers=svc)
            assert first.status_code == 200, first.text
            body = first.json()
            assert body["decision"] == "STEP_UP"
            challenge = body["step_up_challenge_id"]
            assert challenge
            url = "/api/transactions/TX-SU-H1-01/step-up-result"

            # a replayed ingest does not mint a second challenge
            again = c.post("/api/transactions", json=_step_up_tx("TX-SU-H1-01"), headers=svc)
            assert again.json().get("step_up_challenge_id") is None

            # an analyst session cannot report OTP outcomes
            jwt_try = c.post(
                url,
                json={"challenge_id": challenge, "success": True},
                headers=_bearer("admin", "admin"),
            )
            assert jwt_try.status_code == 401
            # the old body (no challenge) is rejected, a guessed id too
            assert c.post(url, json={"success": True}, headers=svc).status_code == 422
            forged = c.post(url, json={"challenge_id": "guess", "success": True}, headers=svc)
            assert forged.status_code == 409 and "Step-up" in forged.json()["detail"]

            ok = c.post(url, json={"challenge_id": challenge, "success": True}, headers=svc)
            assert ok.status_code == 200, ok.text
            assert ok.json()["success"] is True

            replay = c.post(url, json={"challenge_id": challenge, "success": True}, headers=svc)
            assert replay.status_code == 409

    def test_h1_challenge_is_bound_to_its_transaction(self, demo_env: Path) -> None:
        svc = {"X-API-Key": SVC}
        with TestClient(create_app()) as c:
            first = c.post("/api/transactions", json=_step_up_tx("TX-SU-H1-02"), headers=svc)
            challenge = first.json()["step_up_challenge_id"]
            wrong = c.post(
                "/api/transactions/TX-OTHER-0001/step-up-result",
                json={"challenge_id": challenge, "success": True},
                headers=svc,
            )
            assert wrong.status_code == 409

    async def test_h1_store_is_single_use_and_one_per_transaction(self) -> None:
        from app.security.challenges import StepUpChallengeStore

        store = StepUpChallengeStore()
        issued = await store.issue("TX-1", "CUST-1")
        assert issued is not None
        assert await store.issue("TX-1", "CUST-1") is None
        got = await store.consume(issued.challenge_id)
        assert got is not None and got.customer_id == "CUST-1"
        assert await store.consume(issued.challenge_id) is None
        assert await store.consume("") is None

    async def test_h1_pipeline_refuses_other_customer(self, demo_env: Path) -> None:
        from app.pipeline import build_pipeline

        p = await build_pipeline(get_settings())
        await p.start()
        try:
            tx = _step_up_tx("TX-SU-H1-03")
            assert (await p.ingest(tx))["decision"] == "STEP_UP"
            with pytest.raises(ValueError, match="başka bir müşteri"):
                await p.step_up_result(
                    "TX-SU-H1-03", success=True, actor="svc", expected_customer="CUST-XXXX"
                )
        finally:
            await p.stop()


# --- H2: analyst JWTs cannot ingest in prod; scenarios are demo-only -------------------
class TestProdSurface:
    def test_h2_prod_ingest_refuses_analyst_jwt(self, prod_env: Path) -> None:
        with TestClient(create_app()) as c:
            tx = {**_step_up_tx("TX-PROD-JWT"), "amount": 100}
            r = c.post("/api/transactions", json=tx, headers=_bearer("admin", "admin"))
            assert r.status_code == 401 and "servis" in r.json()["detail"]
            ok = c.post("/api/transactions", json=tx, headers={"X-API-Key": SVC})
            assert ok.status_code == 200, ok.text

    def test_h2_scenarios_not_mounted_in_prod(self, prod_env: Path) -> None:
        with TestClient(create_app()) as c:
            h = _bearer("admin", "admin")
            assert c.get("/api/scenarios", headers=h).status_code == 404
            assert c.post("/api/scenarios/smurfing", headers=h).status_code == 404

    def test_h2_scenarios_need_senior_outside_prod(self, demo_env: Path) -> None:
        with TestClient(create_app()) as c:
            analyst = c.get("/api/scenarios", headers=_bearer("analist", "analist"))
            assert analyst.status_code == 403
            senior = c.get("/api/scenarios", headers=_bearer("kidemli_analist", "kidemli_analist"))
            assert senior.status_code == 200


# --- M7: rate-limit key and 401 counting --------------------------------------------------
class TestRateLimits:
    def test_m7_key_is_ip_until_authenticated(self) -> None:
        from starlette.requests import Request

        from app.security.ratelimit import principal_rate_key

        def req(headers: dict[str, str]) -> Request:
            raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
            return Request({"type": "http", "headers": raw, "client": ("10.0.0.9", 1)})

        # rotating credentials does not escape the IP bucket before authentication
        assert principal_rate_key(req({"X-API-Key": "a"})) == "ip:10.0.0.9"
        assert principal_rate_key(req({"Authorization": "Bearer b"})) == "ip:10.0.0.9"
        authed = req({})
        authed.state.principal = Principal("ingest-service", "service", via="api_key")
        assert principal_rate_key(authed) == "api_key:ingest-service"

    def test_m7_repeated_401s_are_throttled(
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURES", "3/minute")
        get_settings.cache_clear()
        with TestClient(create_app()) as c:
            codes = [
                c.get("/api/cases", headers={"Authorization": f"Bearer guess-{i}"}).status_code
                for i in range(5)
            ]
            assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
            # the block applies to the client, whatever it presents next
            assert c.get("/api/health").status_code == 429


def test_hsts_only_in_prod(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "strict-transport-security" not in TestClient(create_app()).get("/api/health").headers
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("JWT_SECRET", "prod-signing-material-" + "x" * 24)
    monkeypatch.setenv("CONSORTIUM_SALT", "prod-consortium-" + "y" * 24)
    monkeypatch.setenv("SEED_DEMO_USERS", "false")
    get_settings.cache_clear()
    r = TestClient(create_app()).get("/api/health")
    assert r.headers["strict-transport-security"].startswith("max-age=")


# --- L3 / L8: export hygiene -------------------------------------------------------------
def test_l3_audit_csv_escapes_formula_cells() -> None:
    from app.api.admin import audit_csv

    text = audit_csv(
        [
            {"id": 1, "transaction_id": '=HYPERLINK("http://x")', "decision": "+cmd"},
            {"id": 2, "transaction_id": "@SUM(A1)", "decision": "\tBLOKE", "risk_score": -0.5},
        ]
    )
    assert '"\'=HYPERLINK(""http://x"")"' in text and '"\'+cmd"' in text
    assert '"\'@SUM(A1)"' in text and '"\'\tBLOKE"' in text
    assert '"-0.5"' in text  # numbers are not formulas


def test_l8_content_disposition_rfc5987() -> None:
    from app.api.downloads import content_disposition

    header = content_disposition('dekont "şüpheli".pdf')
    assert header.startswith('attachment; filename="dekont _supheli_.pdf"')
    assert "filename*=UTF-8''dekont%20%22%C5%9F%C3%BCpheli%22.pdf" in header
    header.encode("latin-1")  # always encodable as an HTTP header
    assert content_disposition("ığ").startswith('attachment; filename="g"')


def test_m7_copilot_is_rate_limited(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_COPILOT", "2/minute")
    get_settings.cache_clear()
    h = _bearer("analist", "analist")
    with TestClient(create_app()) as c:
        codes = [
            c.post("/api/cases/99999/copilot/summary", headers=h).status_code for _ in range(3)
        ]
        assert codes == [404, 404, 429]
