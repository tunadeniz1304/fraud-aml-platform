"""Typology integration tests (F5 exit criterion + DoD #3).

Each attack scenario is injected into a full pipeline running the demo
population and must receive the expected action:

ATO → BLOCK · APP scam → HOLD + warning · mule ring → HOLD + case + graph ring ·
smurfing → HOLD + AML case (then ŞİB).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.config import BASE_DIR, get_settings
from app.features.extractor import CustomerDirectory
from app.pipeline import build_pipeline, rebase_timestamps
from app.scenarios import SCENARIOS, ScenarioError, ScenarioFactory
from app.security.auth import Principal, issue_token

DEMO = BASE_DIR / "data" / "demo"


@pytest.fixture()
def demo_env(app_env, monkeypatch):
    monkeypatch.setenv("FRAUD_CUSTOMERS_PATH", str(DEMO / "customers.json"))
    monkeypatch.setenv("FRAUD_PAYEES_PATH", str(DEMO / "payees.json"))
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("RING_DETECT_INTERVAL_S", "0")
    get_settings.cache_clear()
    yield app_env
    get_settings.cache_clear()


@pytest.fixture()
async def pipeline(demo_env):
    p = await build_pipeline(get_settings())
    await p.start()
    yield p
    await p.stop()


class TestTypologies:
    async def test_ato_blocks(self, pipeline):
        out = await pipeline.run_scenario("ato")
        first = out["results"][0]
        assert first["decision"] == "BLOCK"
        assert any("ele geçirme" in r for r in first["reasons"])
        assert out["cases"][0]["case_type"] == "ATO"
        assert pipeline.accounts.get_status(first["customer_id"]) == "BLOKE"

    async def test_app_scam_holds_with_dynamic_warning(self, pipeline):
        out = await pipeline.run_scenario("app")
        [result] = out["results"]
        assert result["decision"] == "HOLD"
        assert "güvenli hesab" in result["app_warning"]
        assert out["cases"][0]["case_type"] == "APP"
        # "Bu kişiyi tanıyor musunuz?" — customer says no
        answer = await pipeline.cases.customer_confirmation(
            result["transaction_id"], knows_payee=False, note="tanımıyorum"
        )
        assert answer["case_id"] == out["cases"][0]["id"] and "durduruldu" in answer["message"]
        case = await pipeline.cases.get_case(answer["case_id"])
        assert case["status"] == "INCELENIYOR"
        assert case["events"][-1]["event_type"] == "CUSTOMER_CONFIRMATION"

    async def test_mule_ring_holds_opens_case_and_ring(self, pipeline):
        out = await pipeline.run_scenario("mule_ring")
        [result] = out["results"]
        assert result["decision"] == "HOLD" and result["ring_id"]
        case = out["cases"][0]
        assert case["case_type"] == "MULE" and case["ring_id"] == result["ring_id"]
        assert out["rings"] and out["rings"][0]["stats"]["shared_devices"] >= 1
        rings = {r["id"] for r in await pipeline.refresh_rings()}
        assert result["ring_id"] in rings

    async def test_smurfing_holds_as_aml_case(self, pipeline):
        out = await pipeline.run_scenario("smurfing")
        assert all(r["decision"] == "HOLD" for r in out["results"])
        assert any("parçalama" in t for r in out["results"] for t in r["reasons"])
        case = out["cases"][0]
        assert case["case_type"] == "AML"
        # tipping-off: the account is held for review, never visibly blocked
        assert pipeline.accounts.get_status(out["customers"][0]) != "BLOKE"

    async def test_card_testing_is_stopped(self, pipeline):
        out = await pipeline.run_scenario("card_testing")
        assert out["results"][0]["decision"] in ("BLOCK", "HOLD", "STEP_UP")


class TestScenarioFactory:
    def test_scenarios_are_valid_payloads(self):
        from app.api.schemas import TransactionIn

        directory = CustomerDirectory.from_file(DEMO / "customers.json")
        factory = ScenarioFactory(directory, seed=7, now=datetime(2026, 9, 20, 15, 0))
        for name in SCENARIOS:
            scenario = factory.build(name)
            assert scenario.key_ids and scenario.expected == SCENARIOS[name]["expected"]
            for tx in scenario.transactions:
                TransactionIn.model_validate(tx)
            assert scenario.as_dict()["transactions"] == len(scenario.transactions)
        with pytest.raises(ScenarioError):
            factory.build("tsunami")
        with pytest.raises(ScenarioError):
            factory.build("ato", customer_id="NOPE")
        chosen = factory.build("app", customer_id="CUST-S0001")
        assert chosen.customers == ["CUST-S0001"]

    def test_mule_ring_needs_ibans(self):
        directory = CustomerDirectory.from_file(get_settings().resolved_customers_path)
        with pytest.raises(ScenarioError, match="IBAN"):
            ScenarioFactory(directory, seed=1).build("mule_ring")

    def test_rebase_timestamps(self):
        txs = [{"ts": "2026-06-01T10:00:00"}, {"ts": "2026-06-03T10:00:00"}]
        end = datetime(2026, 9, 25, 12, 0)
        out = rebase_timestamps(txs, end=end)
        assert out[1]["ts"] == "2026-09-25T11:59:00" and out[0]["ts"] == "2026-09-23T11:59:00"
        assert rebase_timestamps([]) == []


class TestNetworkApi:
    def test_endpoints(self, demo_env):
        token, _ = issue_token(Principal("analist", "analist", "analist"))
        h = {"Authorization": f"Bearer {token}"}
        with TestClient(create_app()) as client:
            listed = client.get("/api/scenarios", headers=h).json()
            assert {s["name"] for s in listed} == set(SCENARIOS)
            assert client.post("/api/scenarios/nope", headers=h).status_code == 404
            run = client.post("/api/scenarios/mule_ring", headers=h)
            assert run.status_code == 200, run.text
            body = run.json()
            mule = body["results"][0]["customer_id"]
            graph = client.get(f"/api/graph/customer/{mule}?hops=2", headers=h).json()
            assert any(n["data"]["center"] for n in graph["nodes"]) and graph["edges"]
            assert client.get("/api/graph/customer/NOPE", headers=h).status_code == 404
            assert client.get("/api/graph/stats", headers=h).json()["nodes"] > 0
            detected = client.post("/api/graph/rings/detect", headers=h).json()
            assert detected
            stored = client.get("/api/graph/rings", headers=h).json()
            assert {r["id"] for r in detected} <= {r["id"] for r in stored}

            customers = CustomerDirectory.from_file(DEMO / "customers.json").records()
            payee = customers[0]
            cop = client.get(
                "/api/cop", params={"iban": payee["iban"], "name": payee["name"]}, headers=h
            ).json()
            assert cop["result"] == "EXACT"
            miss = client.get(
                "/api/cop", params={"iban": payee["iban"], "name": "Başka Biri"}, headers=h
            ).json()
            assert miss["result"] == "NO_MATCH" and miss["registered_name_masked"]

            app_run = client.post("/api/scenarios/app", headers=h).json()
            tx_id = app_run["results"][0]["transaction_id"]
            ok = client.post(f"/api/app/confirm/{tx_id}", json={"knows_payee": True}, headers=h)
            assert ok.status_code == 200 and ok.json()["knows_payee"] is True
            missing = client.post("/api/app/confirm/NOPE", json={"knows_payee": True}, headers=h)
            assert missing.status_code == 404

    def test_scenario_conflict_without_population(self, app_env, monkeypatch):
        monkeypatch.setenv("STREAM_MODE", "off")
        get_settings.cache_clear()
        token, _ = issue_token(Principal("analist", "analist", "analist"))
        with TestClient(create_app()) as client:
            r = client.post(
                "/api/scenarios/mule_ring", headers={"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 409


async def test_worker_ring_detection_from_db(pipeline):
    from app.graph.batch import detect_and_store

    await pipeline.run_scenario("mule_ring")
    await pipeline.writer.flush()
    result = await detect_and_store(pipeline.db)
    assert result["rings"] >= 1 and result["top"].startswith("RING-")
