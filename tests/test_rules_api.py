"""Rule studio / policy API: CRUD, RBAC, validation, backtest, hot reload, audit."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.security.auth import Principal, issue_token


def _headers(role: str) -> dict[str, str]:
    token, _ = issue_token(Principal(role, role, role))  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {token}"}


ANALYST, SENIOR, ADMIN = _headers("analist"), _headers("kidemli_analist"), _headers("admin")

NEW_RULE = {
    "id": "R_TEST_BIG",
    "name": "Test büyük tutar",
    "when": "amount_try >= 100000",
    "score": 0.4,
    "reason_template": "Tutar {amount_try:.0f} TL",
    "severity": "high",
    "tags": ["test"],
}


@pytest.fixture()
def client(app_env):
    with TestClient(create_app()) as c:
        yield c


class TestRuleCrud:
    def test_list_contains_seeded_core_rules(self, client):
        r = client.get("/api/rules", headers=ANALYST)
        assert r.status_code == 200
        body = r.json()
        ids = {rule["id"] for rule in body["rules"]}
        assert "R_STRUCTURING" in ids and len(ids) >= 22
        assert body["version"].startswith("rs-") and "amount_ratio" in body["fields"]
        detail = client.get("/api/rules/R_STRUCTURING", headers=ANALYST).json()
        assert detail["rule"]["tags"] == ["aml"] and detail["history"][0]["created_by"] == "seed"
        assert client.get("/api/rules/R_NOPE", headers=ANALYST).status_code == 404

    def test_rbac(self, client):
        assert client.get("/api/rules").status_code == 401
        assert client.post("/api/rules", json=NEW_RULE, headers=ANALYST).status_code == 403
        assert (
            client.put(
                "/api/policy/thresholds",
                json={"step_up": 0.3, "hold": 0.5, "block": 0.8},
                headers=SENIOR,
            ).status_code
            == 403
        )

    def test_create_update_disable_with_versions_reload_and_audit(self, client):
        before = client.get("/api/policy", headers=ANALYST).json()["ruleset_version"]
        r = client.post("/api/rules", json=NEW_RULE, headers=SENIOR)
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["rule"]["version"] == 1 and created["ruleset_version"] != before
        assert client.post("/api/rules", json=NEW_RULE, headers=SENIOR).status_code == 409

        updated = client.put(
            "/api/rules/R_TEST_BIG", json={**NEW_RULE, "score": 0.6}, headers=SENIOR
        )
        assert updated.status_code == 200 and updated.json()["rule"]["version"] == 2
        assert client.put("/api/rules/R_OTHER", json=NEW_RULE, headers=SENIOR).status_code == 422
        missing = {**NEW_RULE, "id": "R_MISSING"}
        assert client.put("/api/rules/R_MISSING", json=missing, headers=SENIOR).status_code == 404

        disabled = client.delete("/api/rules/R_TEST_BIG", headers=SENIOR)
        assert disabled.status_code == 200 and disabled.json()["rule"]["enabled"] is False
        assert client.delete("/api/rules/R_NOPE", headers=SENIOR).status_code == 404
        history = client.get("/api/rules/R_TEST_BIG", headers=ANALYST).json()["history"]
        assert [h["version"] for h in history] == [3, 2, 1]

        audit = client.get("/api/audit?limit=50", headers=ANALYST).json()
        events = {row["event_type"] for row in audit}
        assert {"RULE_CREATE", "RULE_UPDATE", "RULE_DISABLE"} <= events
        assert client.get("/api/audit/verify", headers=ANALYST).json()["ok"] is True

    def test_new_rule_changes_live_decisions(self, client):
        rule = {**NEW_RULE, "id": "R_TEST_ALL", "when": "amount_try > 0", "action_hint": "HOLD"}
        assert client.post("/api/rules", json=rule, headers=SENIOR).status_code == 201
        accounts = client.get("/api/accounts", headers=ANALYST).json()
        active = next(a["customer_id"] for a in accounts if a["hesap_durumu"] == "AKTIF")
        tx = {
            "transaction_id": "TX-RULE-1",
            "ts": "2026-09-22T14:00:00",
            "customer_id": active,
            # above rule_floor_hold_min_amount_try: the rule's HOLD floor is not softened
            "amount": 30000,
            "currency": "TRY",
            "device_id": "DEV-9F2A-11",
            "location": "İstanbul",
            "country": "TR",
        }
        r = client.post("/api/transactions", json=tx, headers=ANALYST)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["decision"] in ("HOLD", "BLOCK")
        assert "R_TEST_ALL" in {h["rule_id"] for h in body["rule_hits"]}

    @pytest.mark.parametrize(
        ("patch", "fragment"),
        [
            ({"when": "__import__('os')"}, "yalnızca"),
            ({"when": "amount_ratoi > 1"}, "bilinmeyen alan"),
            ({"when": "a.b > 1"}, "izin verilmeyen"),
        ],
    )
    def test_invalid_expression_is_422(self, client, patch, fragment):
        r = client.post("/api/rules", json={**NEW_RULE, **patch}, headers=SENIOR)
        assert r.status_code == 422 and fragment in r.json()["detail"]

    def test_schema_rejects_bad_fields(self, client):
        bad = {**NEW_RULE, "score": 2}
        assert client.post("/api/rules", json=bad, headers=SENIOR).status_code == 422
        bad = {**NEW_RULE, "extra": 1}
        assert client.post("/api/rules", json=bad, headers=SENIOR).status_code == 422


class TestBacktest:
    def test_simulate_on_db_decisions(self, client):
        r = client.post(
            "/api/rules/R_EXTREME_AMOUNT/simulate", json={"source": "db"}, headers=ANALYST
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "db" and body["evaluated"] >= 5  # scored demo decisions
        assert body["alerts"] >= 1 and body["precision"] is None  # no analyst labels yet
        what_if = client.post(
            "/api/rules/R_EXTREME_AMOUNT/simulate",
            json={"source": "db", "when": "amount_try > 0"},
            headers=ANALYST,
        ).json()
        assert what_if["alerts"] == what_if["evaluated"]
        assert client.post("/api/rules/R_NOPE/simulate", headers=ANALYST).status_code == 404
        bad = client.post(
            "/api/rules/R_EXTREME_AMOUNT/simulate", json={"when": "x >"}, headers=ANALYST
        )
        assert bad.status_code == 422

    def test_simulate_on_synthetic_backtest_file(self, client, app_env, monkeypatch):
        from app.config import get_settings

        generated = app_env / "data" / "generated"
        generated.mkdir(parents=True)
        rows = [
            {"id": f"x{i}", "y": int(i % 4 == 0), "f": {"amount_try": 1000.0 * i}}
            for i in range(40)
        ]
        (generated / "backtest.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
        )
        monkeypatch.setattr(get_settings(), "data_dir", app_env / "data")
        draft = {**NEW_RULE, "when": "amount_try >= 20000", "source": "synthetic"}
        body = client.post("/api/rules/simulate", json=draft, headers=ANALYST).json()
        assert body["source"] == "synthetic" and body["evaluated"] == 40
        assert body["alerts"] == 20 and body["true_positives"] == 5
        assert body["precision"] == 0.25 and body["recall"] == 0.5
        bad = client.post("/api/rules/simulate", json={**draft, "when": "zzz > 1"}, headers=ANALYST)
        assert bad.status_code == 422


class TestPolicyApi:
    def test_get_and_set_thresholds(self, client):
        policy = client.get("/api/policy", headers=ANALYST).json()
        assert policy["thresholds"] == {"step_up": 0.35, "hold": 0.6, "block": 0.85}
        assert policy["model_version"] == "fraud_gbm_v3" and policy["stacker"]["coef"]
        new = {"step_up": 0.3, "hold": 0.5, "block": 0.8}
        r = client.put("/api/policy/thresholds", json=new, headers=ADMIN)
        assert r.status_code == 200 and r.json()["thresholds"] == new
        bad = client.put(
            "/api/policy/thresholds",
            json={"step_up": 0.7, "hold": 0.5, "block": 0.8},
            headers=ADMIN,
        )
        assert bad.status_code == 422
        audit = client.get("/api/audit?limit=20", headers=ANALYST).json()
        assert any(row["event_type"] == "POLICY_THRESHOLDS" for row in audit)

    def test_models_listing(self, client):
        body = client.get("/api/models", headers=ANALYST).json()
        assert body["champion"] == "fraud_gbm_v3"
        assert body["models"][0]["metrics"]["pr_auc"] > 0.6
