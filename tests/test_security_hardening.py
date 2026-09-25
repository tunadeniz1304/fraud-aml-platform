"""Regression tests for the security audit findings (H1, H2, M7 …).

Each test names the finding it pins down.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.dashboard import create_app
from app.cases.service import CaseError, CaseService, MakerCheckerError
from app.config import BASE_DIR, get_settings
from app.db.models import Approval
from app.security.auth import BREAK_GLASS_USERNAME, Principal, UserDirectory, issue_token

DEMO = BASE_DIR / "data" / "demo"
SVC = "svc-credential-for-tests-" + "0" * 8 + "01"


def _bearer(user: str, role: str) -> dict[str, str]:
    token, _ = issue_token(Principal(user, role, user))  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
async def service(store: Any) -> CaseService:
    return CaseService(store.db, accounts=store.accounts, writer=store.writer)


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


def _set_prod_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-2 A9 prod requirements: keyed audit chain, shared rate-limit storage."""
    monkeypatch.setenv("AUDIT_HMAC_KEY", "prod-audit-chain-" + "k" * 24)
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "redis://127.0.0.1:6379/1")


@pytest.fixture()
def prod_env(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("JWT_SECRET", "prod-signing-material-" + "x" * 24)
    monkeypatch.setenv("CONSORTIUM_SALT", "prod-consortium-" + "y" * 24)
    monkeypatch.setenv("SEED_DEMO_USERS", "false")
    _set_prod_extras(monkeypatch)
    get_settings.cache_clear()
    return demo_env


def _step_up_tx(tx_id: str) -> dict[str, Any]:
    """New device + new payee at 6x the usual amount -> STEP_UP (see test_audit_v2 A2)."""
    c = json.loads((DEMO / "customers.json").read_text(encoding="utf-8"))[5]
    avg = float(c.get("avg_amount") or 1000)
    return {
        "transaction_id": tx_id,
        # a fixed daytime hour: the evening/night rules must not tip it to HOLD
        "ts": datetime.now().replace(hour=11, minute=0, second=0, microsecond=0).isoformat(),
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
            # the block applies to unauthenticated requests from the client
            # address; probes and valid sessions are exempt (A8, round 2)
            assert c.get("/api/cases").status_code == 429
            assert c.get("/api/health").status_code == 200


def test_hsts_only_in_prod(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "strict-transport-security" not in TestClient(create_app()).get("/api/health").headers
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("JWT_SECRET", "prod-signing-material-" + "x" * 24)
    monkeypatch.setenv("CONSORTIUM_SALT", "prod-consortium-" + "y" * 24)
    monkeypatch.setenv("SEED_DEMO_USERS", "false")
    _set_prod_extras(monkeypatch)
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


# --- M8: short-lived, revocable sessions re-checked against the user store -----------------
class TestSessions:
    def test_m8_default_ttl_is_one_hour_and_tokens_have_jti(self, app_env: Path) -> None:
        from app.security.auth import decode_token

        token, ttl = issue_token(Principal("analist", "analist", "a"))
        assert ttl == 3600
        assert decode_token(token).token_id

    def test_m8_logout_revokes_the_token(self, app_env: Path) -> None:
        with TestClient(create_app()) as c:
            h = _bearer("analist", "analist")
            assert c.get("/api/auth/me", headers=h).status_code == 200
            assert c.post("/api/auth/logout", headers=h).json() == {"logged_out": True}
            after = c.get("/api/auth/me", headers=h)
            assert after.status_code == 401 and "sonlandırıldı" in after.json()["detail"]
            # other sessions of the same user are unaffected
            assert c.get("/api/auth/me", headers=_bearer("analist", "analist")).status_code == 200

    def test_m8_role_comes_from_the_user_store(self, app_env: Path) -> None:
        with TestClient(create_app()) as c:
            h = _bearer("kidemli_analist", "kidemli_analist")
            assert c.get("/api/auth/me", headers=h).json()["role"] == "kidemli_analist"
            users = c.app.state.users  # type: ignore[attr-defined]
            users.add("kidemli_analist", "analist", "Demoted", "irrelevant-pw")
            assert c.get("/api/auth/me", headers=h).json()["role"] == "analist"
            del users.users["kidemli_analist"]
            gone = c.get("/api/auth/me", headers=h)
            assert gone.status_code == 401 and "bulunamadı" in gone.json()["detail"]
            # a token forged for a role the user never had is downgraded too
            forged = _bearer("analist", "admin")
            assert c.get("/api/auth/me", headers=forged).json()["role"] == "analist"


class TestApprovals:
    def test_h5_break_glass_token_cannot_decide(
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ADMIN_TOKEN", "break-glass-credential-for-tests")
        glass = {"Authorization": "Bearer break-glass-credential-for-tests"}
        with TestClient(create_app()) as c:
            c.post("/api/admin/accounts/CUST-0003/status?status=BLOKE", headers=glass)
            # L11 (round 2): break-glass cannot be the maker either
            refused = c.post("/api/admin/accounts/CUST-0003/status?status=AKTIF", headers=glass)
            assert refused.status_code == 403 and "Break-glass" in refused.json()["detail"]
            req = c.post(
                "/api/admin/accounts/CUST-0003/status?status=AKTIF",
                headers=_bearer("admin", "admin"),
            )
            approval = req.json()["approval"]
            assert approval["requested_by"] != BREAK_GLASS_USERNAME
            denied = c.post(f"/api/approvals/{approval['id']}/approve", headers=glass)
            assert denied.status_code == 403 and "break-glass" in denied.json()["detail"]
            ok = c.post(
                f"/api/approvals/{approval['id']}/approve",
                headers=_bearer("kidemli_analist", "kidemli_analist"),
            )
            assert ok.status_code == 200
        with pytest.raises(ValueError, match="':'"):
            UserDirectory().add(BREAK_GLASS_USERNAME, "admin", "x", "pw-irrelevant")

    async def test_m3_minimum_approver_role_per_kind(self, service: CaseService) -> None:
        req = await service.request_approval("MODEL_PROMOTE", "fraud_gbm_v9", {}, "admin")
        with pytest.raises(MakerCheckerError, match="admin"):
            await service.decide_approval(
                req["id"], approve=True, actor="kidemli_analist", role="kidemli_analist"
            )
        unblock = await service.request_approval("UNBLOCK", "CUST-0009", {}, "analist")
        with pytest.raises(MakerCheckerError, match="kidemli_analist"):
            await service.decide_approval(unblock["id"], approve=False, actor="a2", role="analist")
        # four-eyes compares the stable id case-insensitively
        with pytest.raises(MakerCheckerError, match="dört göz"):
            await service.decide_approval(
                unblock["id"], approve=False, actor="ANALIST", role="admin"
            )

    async def test_m2_decision_is_claimed_once_and_reverted_on_handler_failure(
        self, service: CaseService
    ) -> None:
        async def boom(_approval: dict[str, Any], _actor: str) -> dict[str, Any]:
            raise RuntimeError("handler failed")

        service.handlers["MODEL_PROMOTE"] = boom
        req = await service.request_approval("MODEL_PROMOTE", "fraud_gbm_v9", {}, "admin")
        with pytest.raises(RuntimeError):
            await service.decide_approval(req["id"], approve=True, actor="admin2", role="admin")
        (after,) = await service.list_approvals()
        assert after["status"] == "BEKLIYOR" and after["decided_by"] is None

        calls: list[str] = []

        async def ok(_approval: dict[str, Any], actor: str) -> dict[str, Any]:
            calls.append(actor)
            return {"done": True}

        service.handlers["MODEL_PROMOTE"] = ok
        results = await asyncio.gather(
            service.decide_approval(req["id"], approve=True, actor="admin2", role="admin"),
            service.decide_approval(req["id"], approve=True, actor="admin3", role="admin"),
            return_exceptions=True,
        )
        assert sum(isinstance(r, dict) for r in results) == 1
        assert sum(isinstance(r, CaseError) for r in results) == 1
        assert len(calls) == 1

    async def test_l6_one_pending_request_per_target_in_the_schema(
        self, service: CaseService
    ) -> None:
        async with service.db.transaction() as session:
            session.add(Approval(kind="UNBLOCK", target_id="C1", payload={}, requested_by="a"))
        with pytest.raises(IntegrityError):
            async with service.db.transaction() as session:
                session.add(Approval(kind="UNBLOCK", target_id="C1", payload={}, requested_by="b"))
        with pytest.raises(CaseError, match="bekleyen"):
            await service.request_approval("UNBLOCK", "C1", {}, "b")


class TestCaseClosure:
    async def test_m5_fraud_closure_and_reopen_need_a_senior(self, service: CaseService) -> None:
        case_id = await service.on_decision(
            {
                "transaction_id": "TX-M5-1",
                "customer_id": "CUST-0001",
                "decision": "HOLD",
                "risk_score": 0.7,
                "amount_try": 10_000,
                "reason_codes": [],
                "rule_hits": [],
            }
        )
        with pytest.raises(MakerCheckerError, match="kıdemli"):
            await service.decide(case_id, "FRAUD", "analist", role="analist")
        closed = await service.decide(case_id, "FRAUD", "kidemli_analist", role="kidemli_analist")
        assert closed["status"] == "KAPANDI_FRAUD"
        # a junior cannot reopen it and re-close it as TEMIZ either
        with pytest.raises(MakerCheckerError, match="yeniden açmak"):
            await service.set_status(case_id, "INCELENIYOR", "analist", role="analist")
        reopened = await service.set_status(case_id, "INCELENIYOR", "admin", role="admin")
        assert reopened["status"] == "INCELENIYOR"


# --- H6 / M17: fail-fast production configuration --------------------------------------
@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"JWT_SECRET": ""}, "JWT_SECRET tanımlı olmalıdır"),
        ({"JWT_SECRET": "kısa-anahtar"}, "en az 32 karakter"),
        ({"JWT_SECRET": "change-me-" + "z" * 30}, "örnek/varsayılan"),
        ({"SEED_DEMO_USERS": "true"}, "demo kullanıcılar"),
        ({"CONSORTIUM_SALT": "anil3-consortium-demo"}, "demo konsorsiyum tuzu"),
    ],
)
def test_h6_prod_refuses_to_start_with_demo_secrets(
    prod_env: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], fragment: str
) -> None:
    for key, value in env.items():
        if value:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match=fragment):
        create_app()


def test_h6_prod_starts_with_real_secrets(prod_env: Path) -> None:
    from app.security.startup import startup_problems

    assert startup_problems(get_settings()) == []
    assert create_app() is not None


def test_h6_several_workers_need_a_shared_jwt_secret(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"):
        create_app()
    monkeypatch.setenv("JWT_SECRET", "shared-signing-material-" + "w" * 16)
    get_settings.cache_clear()
    # several workers also need shared state (A9, round 2)
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        create_app()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    get_settings.cache_clear()
    assert create_app() is not None


def test_h6_m17_compose_requires_secrets_and_closes_grafana() -> None:
    compose = (BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert "JWT_SECRET: ${JWT_SECRET:?" in compose
    assert "SEED_DEMO_USERS: ${SEED_DEMO_USERS:-false}" in compose
    assert 'GF_AUTH_ANONYMOUS_ENABLED: "false"' in compose
    assert 'GF_AUTH_ANONYMOUS_ENABLED: "true"' not in compose
    example = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
    for key in ("JWT_SECRET=", "CONSORTIUM_SALT=", "WEB_CONCURRENCY=", "SEED_DEMO_USERS="):
        assert f"\n{key}\n" in example


# --- M11: the audit trail is not for every analyst -------------------------------------
def test_m11_audit_log_needs_a_senior_analyst(demo_env: Path) -> None:
    with TestClient(create_app()) as c:
        analyst = _bearer("analist", "analist")
        assert c.get("/api/audit", headers=analyst).status_code == 403
        assert c.get("/api/bus/dlq", headers=analyst).status_code == 403
        senior = _bearer("kidemli_analist", "kidemli_analist")
        assert c.get("/api/audit", headers=senior).status_code == 200
        assert c.get("/api/bus/dlq", headers=senior).status_code == 200
        # integrity check stays available to every analyst
        assert c.get("/api/audit/verify", headers=analyst).status_code == 200
