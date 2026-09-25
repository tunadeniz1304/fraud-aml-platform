"""Copilot (P1.5) + model governance (P1.6) tests — never touch the network."""

from __future__ import annotations

import json
import random

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.config import BASE_DIR, get_settings
from app.copilot.schemas import CaseSummary, citation_validator
from app.llm.config import LLMSettings
from app.llm.service import LLMService
from app.llm.types import ChatResponse, LLMCallError, ToolCall
from app.ml.monitoring import DriftMonitor
from app.pipeline import build_pipeline
from app.security.auth import Principal, issue_token

DEMO = BASE_DIR / "data" / "demo"


def _h(user: str, role: str) -> dict[str, str]:
    token, _ = issue_token(Principal(user, role, user))  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {token}"}


ANALYST, SENIOR, ADMIN = (
    _h("analist", "analist"),
    _h("kidemli", "kidemli_analist"),
    _h("admin", "admin"),
)


@pytest.fixture()
def demo_env(app_env, monkeypatch):
    monkeypatch.setenv("FRAUD_CUSTOMERS_PATH", str(DEMO / "customers.json"))
    monkeypatch.setenv("FRAUD_PAYEES_PATH", str(DEMO / "payees.json"))
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("RING_DETECT_INTERVAL_S", "0")
    get_settings.cache_clear()
    yield app_env
    get_settings.cache_clear()


class ScriptedBackend:
    """Fake live backend: scripted tool calls, then a final answer per task."""

    name, model = "fake", "fake-live"

    def __init__(self, case_id: int, *, cite: str | None = None, fail: str | None = None):
        self.case_id, self.cite, self.fail = case_id, cite, fail
        self.requests: list = []

    async def chat(self, request):
        self.requests.append(request)
        if self.fail and request.task == "copilot_investigate":
            raise LLMCallError(self.fail)  # type: ignore[arg-type]
        if request.task == "copilot_investigate":
            done = sum(1 for m in request.messages if m.get("role") == "tool")
            plan = [
                ("get_case", {"case_id": self.case_id}),
                ("get_rule_hits", {"transaction_id": "X"}),
            ]
            if done < len(plan):
                name, args = plan[done]
                return ChatResponse("", [ToolCall(f"c{done}", name, args)], model=self.model)
            return ChatResponse("Bitti", model=self.model)
        if request.task == "copilot_summary":
            bullets = [
                {"text": f"Kanıt maddesi {i}", "citations": [self.cite or f"CASE-{self.case_id}"]}
                for i in range(5)
            ]
            return ChatResponse(json.dumps({"bullets": bullets, "risk_assessment": "Yüksek risk."}))
        return ChatResponse("{}")

    async def stream(self, request):  # pragma: no cover - not used here
        yield "x"


async def _case_pipeline(demo_env, scenario="smurfing"):
    p = await build_pipeline(get_settings())
    await p.start()
    out = await p.run_scenario(scenario)
    return p, out["cases"][0]["id"]


class TestAgent:
    async def test_demo_outputs_are_cited_and_complete(self, demo_env):
        p, case_id = await _case_pipeline(demo_env)
        try:
            summary, inv = await p.copilot.summarize(case_id)
            assert summary.llm_mode == "demo" and len(summary.output.bullets) == 5
            assert [t["tool"] for t in inv.trace][:3] == [
                "get_case",
                "get_customer_profile",
                "get_recent_transactions",
            ]
            assert len(inv.trace) <= 6
            cited = {c for b in summary.output.bullets for c in b.citations}
            assert cited <= inv.evidence_ids
            rec, _ = await p.copilot.recommend(case_id)
            assert rec.output.decision in ("FRAUD", "TEMIZ", "INCELEMEYE_DEVAM")
            # the AML case got an automatic ŞİB draft (DoD: smurfing → vaka + ŞİB taslağı)
            case = await p.cases.get_case(case_id)
            draft = case["sib_draft"]
            assert case["sib_status"] == "SIB_TASLAK" and draft["islemler"]
            for field in ("kim", "ne", "ne_zaman", "nerede", "neden", "supheli_islem_tipi"):
                assert draft[field]
            assert "tipping-off" in draft["tipping_off_uyarisi"]
            assert "Kara para" in draft["supheli_islem_tipi"]
            triage = await p.copilot.triage_text("Kripto yatırım fırsatı")
            assert triage.output.label == "yatirim"
        finally:
            await p.stop()

    async def test_live_tool_loop_and_citation_guard(self, demo_env):
        p, case_id = await _case_pipeline(demo_env)
        try:
            for cite, expected_mode in ((None, "live"), ("TX-UYDURMA-999", "fallback")):
                backend = ScriptedBackend(case_id, cite=cite)
                p.copilot.llm = LLMService(
                    LLMSettings(mode="live", api_key="test-key-123"), live_backend=backend
                )
                result, inv = await p.copilot.summarize(case_id)
                assert inv.mode == "live" and [t["tool"] for t in inv.trace] == [
                    "get_case",
                    "get_rule_hits",
                ]
                assert inv.trace[1]["error"]  # unknown transaction -> tool error surfaced as data
                assert result.llm_mode == expected_mode
                if expected_mode == "fallback":
                    assert result.llm_error_kind == "citation_invalid"
                tool_msgs = [
                    m for r in backend.requests for m in r.messages if m.get("role") == "tool"
                ]
                assert tool_msgs and "evidence_ids" in tool_msgs[-1]["content"]
        finally:
            await p.stop()

    @pytest.mark.parametrize(
        ("fail", "protocol", "mode"),
        [("tools_unsupported", "react", "live"), ("timeout", "tools", "fallback")],
    )
    async def test_react_and_failure_fallbacks(self, demo_env, fail, protocol, mode):
        p, case_id = await _case_pipeline(demo_env)
        try:
            backend = ScriptedBackend(case_id, fail=fail)
            if fail == "tools_unsupported":

                async def chat(request, _orig=backend.chat):
                    if request.tools:
                        raise LLMCallError("tools_unsupported")
                    steps = sum(1 for m in request.messages if m["content"].startswith("GÖZLEM"))
                    if steps == 0:
                        return ChatResponse(f'ARAÇ: get_case {{"case_id": {case_id}}}')
                    return ChatResponse("BİTTİ")

                backend.chat = chat  # type: ignore[method-assign]
            p.copilot.llm = LLMService(
                LLMSettings(mode="live", api_key="test-key-123"), live_backend=backend
            )
            inv = await p.copilot.investigate(case_id)
            assert inv.protocol == protocol and inv.mode == mode and "get_case" in inv.evidence
        finally:
            await p.stop()

    def test_citation_validator(self):
        check = citation_validator(["A", "B"])
        ok = CaseSummary.model_validate(
            {
                "bullets": [{"text": "madde bir", "citations": ["A"]}] * 3,
                "risk_assessment": "düşük risk",
            }
        )
        assert check(ok) == []
        assert check({"x": [{"citations": ["A", "Z"]}]}) == ["vakada olmayan kanıt kimliği: Z"]


def ticket(client) -> str:
    """Single-use SSE ticket (EventSource cannot send the bearer header)."""
    return client.post("/api/stream/ticket", headers=ANALYST).json()["ticket"]


class TestApi:
    def test_copilot_endpoints_sib_export_chat_and_models(self, demo_env):
        with TestClient(create_app()) as client:
            run = client.post("/api/scenarios/smurfing", headers=ANALYST).json()
            cid = run["cases"][0]["id"]
            s = client.post(f"/api/cases/{cid}/copilot/summary", headers=ANALYST)
            assert s.status_code == 200 and len(s.json()["summary"]["bullets"]) == 5
            assert s.json()["trace"] and s.json()["llm_mode"] == "demo"
            assert client.get(f"/api/cases/{cid}", headers=ANALYST).json()["summary"]
            r = client.post(f"/api/cases/{cid}/copilot/recommendation", headers=ANALYST).json()
            assert "analist onayı" in r["notice"]
            sib = client.post(f"/api/cases/{cid}/copilot/sib", headers=ANALYST).json()
            assert sib["sib_draft"]["toplam_tutar_try"] > 0
            pdf = client.get(f"/api/cases/{cid}/sib.pdf", headers=ANALYST)
            assert pdf.headers["content-type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
            js = client.get(f"/api/cases/{cid}/sib.json", headers=ANALYST).json()
            assert js["tipping_off_uyarisi"]
            with client.stream(
                "GET", f"/api/cases/{cid}/chat?q=Bu hesap neden riskli?&ticket={ticket(client)}"
            ) as resp:
                lines = [ln for ln in resp.iter_lines() if ln.startswith("data:")]
            events = [json.loads(ln[5:]) for ln in lines]
            assert events[0]["type"] == "trace" and events[-1]["type"] == "done"
            assert "neden riskli" in "".join(e.get("text", "") for e in events)
            for bad in (
                "/api/cases/99999/copilot/summary",
                "/api/cases/99999/copilot/sib",
                "/api/cases/99999/copilot/recommendation",
            ):
                assert client.post(bad, headers=ANALYST).status_code == 404
            assert client.get("/api/cases/99999/sib.pdf", headers=ANALYST).status_code == 404
            missing = client.get(f"/api/cases/99999/chat?q=xx&ticket={ticket(client)}")
            assert missing.status_code == 404

            compare = client.get("/api/models/compare", headers=ANALYST).json()
            assert compare["champion"] == "fraud_gbm_v1" and compare["challenger"] == "fraud_gbm_v2"
            assert compare["online"]["shadow_decisions"] > 0 and "agreement" in compare["online"]
            assert compare["offline"]["challenger"]["pr_auc"] > 0.6
            drift = client.get("/api/models/drift", headers=ANALYST).json()
            assert "psi" in drift and drift["thresholds"]["alert"] == 0.25
            assert isinstance(
                client.get("/api/models/active-learning", headers=ANALYST).json(), list
            )
            assert client.post("/api/models/nope/promote", headers=ADMIN).status_code == 404
            assert (
                client.post("/api/models/fraud_gbm_v2/promote", headers=ANALYST).status_code == 403
            )

    def test_promotion_is_maker_checker(self, demo_env, tmp_path, monkeypatch):
        import shutil

        models = tmp_path / "models"
        shutil.copytree(BASE_DIR / "models", models)
        monkeypatch.setenv("MODELS_DIR", str(models))
        get_settings.cache_clear()
        with TestClient(create_app()) as client:
            req = client.post("/api/models/fraud_gbm_v2/promote", headers=ADMIN)
            assert req.status_code == 202 and req.json()["kind"] == "MODEL_PROMOTE"
            assert (
                client.post(f"/api/approvals/{req.json()['id']}/approve", headers=ADMIN).status_code
                == 403
            )
            ok = client.post(f"/api/approvals/{req.json()['id']}/approve", headers=SENIOR)
            assert ok.status_code == 200 and ok.json()["result"]["champion"] == "fraud_gbm_v2"
            ready = client.get("/api/health/ready").json()
            assert ready["info"]["model"] == "fraud_gbm_v2"
            back = client.put("/api/models/fraud_gbm_v1/challenger", headers=ADMIN)
            assert back.status_code == 200 and back.json()["challenger"] == "fraud_gbm_v1"
            assert (
                client.put("/api/models/fraud_gbm_v2/challenger", headers=ADMIN).status_code == 409
            )


def test_drift_monitor_detects_shift():
    rng = random.Random(0)
    ref_values = [rng.gauss(0, 1) for _ in range(5000)]
    from app.ml import metrics as M

    edges = M.reference_bins(ref_values)
    monitor = DriftMonitor(
        {"score": {"edges": edges, "share": M.distribution(ref_values, edges)}, "bad": {}},
        compute_every=100,
    )
    for _ in range(300):
        monitor.observe({}, rng.gauss(0, 1))
    assert monitor.status()["alerts"] == []
    for _ in range(3000):
        monitor.observe({}, rng.gauss(1.5, 1))
    status = monitor.status()
    assert status["alerts"] == ["score"] and status["psi"]["score"] > 0.25
