"""Analyst workbench API: full case lifecycle, RBAC, maker-checker, observability."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.security.auth import Principal, issue_token


def _headers(user: str, role: str) -> dict[str, str]:
    token, _ = issue_token(Principal(user, role, user))  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {token}"}


ANALYST = _headers("analist", "analist")
ANALYST2 = _headers("analist2", "analist")
SENIOR = _headers("kidemli_analist", "kidemli_analist")
ADMIN = _headers("admin", "admin")

ATO_TX = {
    "transaction_id": "TX-API-ATO",
    "ts": "2026-09-22T03:10:00",
    "customer_id": "CUST-0004",
    "amount": 60000,
    "currency": "EUR",
    "device_id": "DEV-NEW-1",
    "location": "Berlin",
    "country": "DE",
    "ip_address": "45.84.2.2",
    "beneficiary_iban": "TR330006100519786457841326",
    "beneficiary_name": "Ali Veli",
}


@pytest.fixture()
def client(app_env):
    with TestClient(create_app()) as c:
        yield c


def open_case(client) -> dict:
    r = client.post("/api/transactions", json=ATO_TX, headers=ANALYST)
    assert r.status_code == 200 and r.json()["decision"] in ("HOLD", "BLOCK"), r.text
    cases = client.get("/api/cases?customer_id=CUST-0004", headers=ANALYST).json()
    assert cases, "risky transaction must open a case"
    return cases[0]


class TestLifecycle:
    def test_full_case_lifecycle(self, client):
        case = open_case(client)
        cid = case["id"]
        assert case["status"] == "YENI" and case["alert_count"] >= 1
        detail = client.get(f"/api/cases/{cid}", headers=ANALYST).json()
        alert = next(a for a in detail["alerts"] if a["transaction_id"] == "TX-API-ATO")
        assert alert["scoring"]["reason_codes"] and alert["transaction"]["country"] == "DE"

        assigned = client.post(f"/api/cases/{cid}/assign", json={}, headers=ANALYST).json()
        assert assigned["assigned_to"] == "analist" and assigned["status"] == "INCELENIYOR"
        r = client.post(f"/api/cases/{cid}/notes", json={"text": "Müşteri arandı"}, headers=ANALYST)
        assert r.status_code == 201
        evidence = client.post(
            f"/api/cases/{cid}/evidence",
            json={
                "filename": "sms.txt",
                "content_type": "text/plain",
                "content_b64": base64.b64encode(b"OTP paylasildi").decode(),
            },
            headers=ANALYST,
        ).json()
        download = client.get(f"/api/cases/{cid}/evidence/{evidence['id']}", headers=ANALYST)
        assert download.content == b"OTP paylasildi"
        assert download.headers["x-content-sha256"] == evidence["sha256"]
        bad = client.post(f"/api/cases/{cid}/status", json={"status": "YENI"}, headers=ANALYST)
        assert bad.status_code == 409

        # M5: a FRAUD closure needs a senior analyst
        junior = client.post(
            f"/api/cases/{cid}/decision", json={"outcome": "FRAUD"}, headers=ANALYST
        )
        assert junior.status_code == 403 and "kıdemli" in junior.json()["detail"]
        decided = client.post(
            f"/api/cases/{cid}/decision", json={"outcome": "FRAUD"}, headers=SENIOR
        ).json()
        assert decided["status"] == "KAPANDI_FRAUD"

        sib = {"draft": {"supheli_islem": "TX-API-ATO", "tip": "ATO"}}
        assert client.put(f"/api/cases/{cid}/sib", json=sib, headers=ANALYST).status_code == 200
        req = client.post(f"/api/cases/{cid}/sib/submit", headers=ANALYST)
        assert req.status_code == 202
        approval_id = req.json()["id"]
        pending = client.get("/api/approvals", headers=ANALYST).json()
        assert any(a["id"] == approval_id for a in pending)
        # analyst cannot approve at all; the maker cannot approve their own request
        assert (
            client.post(f"/api/approvals/{approval_id}/approve", headers=ANALYST).status_code == 403
        )
        ok = client.post(
            f"/api/approvals/{approval_id}/approve", json={"note": "uygun"}, headers=SENIOR
        )
        assert ok.status_code == 200, ok.text
        final = client.get(f"/api/cases/{cid}", headers=ANALYST).json()
        assert final["status"] == "SIB_GONDERILDI" and final["sib_draft"]["masak_reference"]

        audit = {
            row["event_type"] for row in client.get("/api/audit?limit=200", headers=SENIOR).json()
        }
        assert {"CASE_OPENED", "CASE_ASSIGNED", "CASE_DECISION", "APPROVAL_DECIDED"} <= audit
        assert client.get("/api/audit/verify", headers=ANALYST).json()["ok"] is True

    def test_unblock_via_maker_checker(self, client):
        client.post("/api/admin/accounts/CUST-0003/status?status=BLOKE", headers=ADMIN)
        r = client.post("/api/admin/accounts/CUST-0003/status?status=AKTIF", headers=ADMIN)
        assert r.status_code == 202 and r.json()["approval"]["kind"] == "UNBLOCK"
        dup = client.post(
            "/api/accounts/CUST-0003/unblock-request", json={"note": "tekrar"}, headers=ANALYST
        )
        assert dup.status_code == 409  # already pending
        approval_id = r.json()["approval"]["id"]
        own = client.post(f"/api/approvals/{approval_id}/approve", headers=ADMIN)
        assert own.status_code == 403 and "dört göz" in own.json()["detail"]
        ok = client.post(f"/api/approvals/{approval_id}/approve", headers=SENIOR)
        assert ok.status_code == 200 and ok.json()["result"]["hesap_durumu"] == "AKTIF"
        accounts = {
            a["customer_id"]: a["hesap_durumu"]
            for a in client.get("/api/accounts", headers=ANALYST).json()
        }
        assert accounts["CUST-0003"] == "AKTIF"
        not_blocked = client.post(
            "/api/accounts/CUST-0003/unblock-request", json={}, headers=ANALYST
        )
        assert not_blocked.status_code == 409
        assert (
            client.post("/api/accounts/NOPE/unblock-request", json={}, headers=ANALYST).status_code
            == 404
        )
        assert (
            client.post("/api/admin/accounts/NOPE/status?status=BLOKE", headers=ADMIN).status_code
            == 404
        )
        assert (
            client.post("/api/admin/accounts/CUST-0003/status?status=X", headers=ADMIN).status_code
            == 422
        )

    def test_unblock_request_by_analyst_and_reject(self, client):
        client.post("/api/admin/accounts/CUST-0005/status?status=BLOKE", headers=ADMIN)
        r = client.post("/api/accounts/CUST-0005/unblock-request", json={}, headers=ANALYST)
        assert r.status_code == 202
        rej = client.post(f"/api/approvals/{r.json()['id']}/reject", headers=SENIOR)
        assert rej.status_code == 200 and rej.json()["status"] == "REDDEDILDI"
        assert client.get("/api/approvals?status=ALL", headers=ANALYST).json()


class TestRbacAndValidation:
    def test_assignment_rules(self, client):
        cid = open_case(client)["id"]
        other = client.post(f"/api/cases/{cid}/assign", json={"assignee": "x"}, headers=ANALYST)
        assert other.status_code == 403
        ok = client.post(f"/api/cases/{cid}/assign", json={"assignee": "analist2"}, headers=SENIOR)
        assert ok.status_code == 200 and ok.json()["assigned_to"] == "analist2"

    def test_auth_and_404s(self, client):
        assert client.get("/api/cases").status_code == 401
        assert client.get("/api/cases/99999", headers=ANALYST).status_code == 404
        assert client.get("/api/cases?status=HEPSI", headers=ANALYST).status_code == 422
        assert client.post("/api/approvals/999/approve", headers=SENIOR).status_code == 404
        bad = client.post(
            "/api/cases/1/evidence",
            json={"filename": "../../etc/passwd", "content_b64": "eA=="},
            headers=ANALYST,
        )
        assert bad.status_code == 422

    def test_listing_alerts_and_stats(self, client):
        open_case(client)
        assert client.get("/api/alerts?limit=5", headers=ANALYST).json()
        stats = client.get("/api/cases/stats", headers=ANALYST).json()
        assert stats["sla"]["open"] >= 1 and sum(stats["by_status"].values()) >= 1
        for order in ("priority", "sla", "masak", "recent"):
            assert (
                client.get(f"/api/cases?order={order}&status=OPEN", headers=ANALYST).status_code
                == 200
            )


class TestPagination:
    def test_page_returns_total_and_disjoint_slices(self, client):
        open_case(client)
        other = {**ATO_TX, "transaction_id": "TX-API-ATO-2", "customer_id": "CUST-0005"}
        assert client.post("/api/transactions", json=other, headers=ANALYST).status_code == 200
        full = client.get("/api/cases/page?limit=200", headers=ANALYST).json()
        assert full["total"] == len(full["items"]) >= 2
        first = client.get("/api/cases/page?limit=1&offset=0", headers=ANALYST).json()
        second = client.get("/api/cases/page?limit=1&offset=1", headers=ANALYST).json()
        assert first["total"] == second["total"] == full["total"]
        assert first["items"][0]["id"] != second["items"][0]["id"]
        assert [first["items"][0]["id"], second["items"][0]["id"]] == [
            c["id"] for c in full["items"][:2]
        ]

    def test_page_filters_and_validates(self, client):
        open_case(client)
        page = client.get("/api/cases/page?customer_id=CUST-0004", headers=ANALYST).json()
        assert page["total"] >= 1
        assert {c["customer_id"] for c in page["items"]} == {"CUST-0004"}
        assert client.get("/api/cases/page?status=NOPE", headers=ANALYST).status_code == 422
        assert client.get("/api/cases/page?limit=500", headers=ANALYST).status_code == 422
        assert client.get("/api/cases/page").status_code == 401
