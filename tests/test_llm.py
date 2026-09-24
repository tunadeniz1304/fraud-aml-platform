"""LLM contract tests (§3) — never touch the network.

Live calls go through the real ``AsyncOpenAI`` SDK wired to an
``httpx.MockTransport``, so request building, JSON mode, error mapping and
fallback are exercised end-to-end without leaving the process.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, SecretStr

from app.llm.backends import OpenAICompatibleBackend
from app.llm.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    KEY_ENV_NAMES,
    LLMConfigError,
    LLMSettings,
    mask_secret,
)
from app.llm.demo import DeterministicLLM, fmt_try
from app.llm.jsonparse import JSONExtractionError, extract_json_object
from app.llm.service import LLMService
from app.llm.tasks import TransactionNarrative, narrate_transaction

FAKE_KEY = "test-key-0123456789abcdef"
ALL_LLM_ENV = (
    *KEY_ENV_NAMES,
    "LLM_BASE_URL",
    "DEEPSEEK_BASE_URL",
    "EVREN_BASE_URL",
    "OPENAI_BASE_URL",
    "LLM_MODEL",
    "DEEPSEEK_MODEL",
    "LLM_MODE",
    "LLM_PROVIDER",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ALL_LLM_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def completion(content: str | None, **message_extra: Any) -> dict[str, Any]:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, **message_extra},
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


class Recorder:
    """httpx handler returning scripted responses and recording requests."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content or b"{}"))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return item(request) if callable(item) else item


def live_service(recorder: Recorder, **settings_kw: Any) -> LLMService:
    settings = LLMSettings(
        api_key=SecretStr(FAKE_KEY), mode="live", max_retries=0, timeout_seconds=5, **settings_kw
    )
    client = AsyncOpenAI(
        api_key=FAKE_KEY,
        base_url="https://llm.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    return LLMService(settings, live_backend=OpenAICompatibleBackend(settings, client=client))


class Answer(BaseModel):
    summary: str
    customer: str = ""
    evidence: list[str] = []


# --- configuration -------------------------------------------------------------
class TestSettings:
    def test_defaults_point_to_evren_deepseek(self, clean_env):
        s = LLMSettings()
        assert s.base_url == DEFAULT_BASE_URL
        assert s.model == DEFAULT_MODEL == "deepseek-v4-flash"
        assert s.mode == "auto" and s.timeout_seconds == 20 and s.max_retries == 2
        assert s.temperature == 0.2 and s.max_tokens == 1200

    def test_key_alias_order_first_non_empty_wins(self, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "k-openai-1")
        clean_env.setenv("EVREN_API_KEY", "k-evren-1")
        assert LLMSettings().api_key.get_secret_value() == "k-evren-1"
        clean_env.setenv("DEEPSEEK_API_KEY", "k-deep-1")
        assert LLMSettings().api_key.get_secret_value() == "k-deep-1"
        clean_env.setenv("LLM_API_KEY", "")  # empty values are ignored
        assert LLMSettings().api_key.get_secret_value() == "k-deep-1"
        clean_env.setenv("LLM_API_KEY", "k-llm-1")
        assert LLMSettings().api_key.get_secret_value() == "k-llm-1"

    def test_base_url_and_model_aliases(self, clean_env):
        clean_env.setenv("OPENAI_BASE_URL", "https://b.example/v1")
        clean_env.setenv("DEEPSEEK_MODEL", "deepseek-x")
        s = LLMSettings()
        assert s.base_url == "https://b.example/v1" and s.model == "deepseek-x"
        clean_env.setenv("LLM_BASE_URL", "https://a.example/v1")
        clean_env.setenv("LLM_MODEL", "m-1")
        s = LLMSettings()
        assert s.base_url_host == "a.example" and s.model == "m-1"

    def test_auto_mode_resolution(self, clean_env):
        assert LLMSettings().resolve_mode() == "demo"
        clean_env.setenv("LLM_API_KEY", FAKE_KEY)
        assert LLMSettings().resolve_mode() == "live"

    def test_forced_demo_even_with_key(self, clean_env):
        clean_env.setenv("LLM_API_KEY", FAKE_KEY)
        clean_env.setenv("LLM_MODE", "demo")
        assert LLMSettings().resolve_mode() == "demo"
        assert LLMService().mode == "demo"

    def test_forced_live_without_key_fails_fast_in_turkish(self, clean_env):
        clean_env.setenv("LLM_MODE", "live")
        with pytest.raises(LLMConfigError, match="anahtarı bulunamadı"):
            LLMService()

    def test_startup_line(self, clean_env):
        assert LLMSettings().startup_line() == "LLM: DEMO modu (anahtar bulunamadı)"
        clean_env.setenv("LLM_API_KEY", FAKE_KEY)
        line = LLMSettings().startup_line()
        assert line == "LLM: CANLI (deepseek-v4-flash @ evren-llmapi.ssyz.org.tr)"
        assert FAKE_KEY not in line
        clean_env.setenv("LLM_MODE", "live")
        clean_env.delenv("LLM_API_KEY")
        assert "HATA" in LLMSettings().startup_line()

    def test_key_never_in_repr_or_status(self, clean_env):
        clean_env.setenv("LLM_API_KEY", FAKE_KEY)
        s = LLMSettings()
        assert FAKE_KEY not in repr(s) and FAKE_KEY not in str(s.model_dump())
        service = LLMService(s, live_backend=DeterministicLLM())
        status = json.dumps(service.status())
        assert FAKE_KEY not in status and "api_key" not in status
        assert service.status()["key_present"] is True

    def test_mask_secret(self):
        assert mask_secret("tk-abcdefghijklmnop1234") == "tk-…1234"
        assert mask_secret(SecretStr("short")) == "***"
        assert mask_secret("") == ""


# --- JSON extraction -------------------------------------------------------------
class TestJSONExtraction:
    @pytest.mark.parametrize(
        "text",
        [
            '{"a": 1}',
            'Tabii! İşte yanıt: {"a": 1} umarım yardımcı olur.',
            '```json\n{"a": 1}\n```',
            'önce {bozuk sonra {"a": 1}',
        ],
    )
    def test_extracts_object(self, text):
        assert extract_json_object(text) == {"a": 1}

    def test_nested_braces_in_strings(self):
        assert extract_json_object('x {"s": "} {", "n": {"k": 2}} y') == {
            "s": "} {",
            "n": {"k": 2},
        }

    @pytest.mark.parametrize("text", ["", "   ", "hiç json yok", "[1, 2]"])
    def test_rejects(self, text):
        with pytest.raises(JSONExtractionError):
            extract_json_object(text)


# --- demo mode ---------------------------------------------------------------------
class TestDemoMode:
    async def test_demo_generate_is_deterministic_and_reasoned(self):
        service = LLMService(LLMSettings(mode="demo"))
        analyzed = {
            "transaction_id": "TX-9",
            "amount": 90000,
            "amount_try": 4_050_000,
            "risk_score": 0.91,
            "risk_explanation": ["bilinmeyen cihaz", "yüksek riskli ülke"],
        }
        first = await narrate_transaction(service, analyzed)
        second = await narrate_transaction(service, analyzed)
        assert first.output == second.output
        assert first.llm_mode == "demo" and first.llm_error_kind is None
        assert first.output.recommended_action == "block"
        assert "bilinmeyen cihaz" in first.output.summary
        assert "4.050.000,00 TL" in first.output.summary

    def test_fmt_try(self):
        assert fmt_try(1234567.8) == "1.234.567,80 TL"

    async def test_demo_stream(self):
        service = LLMService(LLMSettings(mode="demo"))
        events = [
            e
            async for e in service.stream_text(
                task="chat",
                system="s",
                payload={},
                instruction="i",
                demo_context={"question": "Neden riskli?", "facts": ["yeni cihaz"]},
            )
        ]
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        assert "yeni cihaz" in text
        assert events[-1]["type"] == "done" and events[-1]["llm_mode"] == "demo"


# --- live mode via mocked HTTP -------------------------------------------------------------
class TestLiveMode:
    async def test_live_json_mode_success_ignores_reasoning(self):
        rec = Recorder(
            httpx.Response(
                200,
                json=completion(
                    '{"summary": "tamam", "customer": "MUSTERI_1"}',
                    reasoning_content="İÇ DÜŞÜNCE — yok sayılmalı",
                ),
            )
        )
        service = live_service(rec)
        result = await service.generate(
            task="t",
            system="sistem",
            payload={"not": "Ayşe Yılmaz transferi"},
            schema=Answer,
            names=["Ayşe Yılmaz"],
        )
        assert result.llm_mode == "live" and result.llm_error_kind is None
        assert result.output.summary == "tamam"
        assert result.output.customer == "Ayşe Yılmaz"  # pseudonym mapped back
        assert "İÇ DÜŞÜNCE" not in result.output.summary
        assert rec.requests[0]["response_format"] == {"type": "json_object"}
        assert rec.requests[0]["model"] == DEFAULT_MODEL
        assert result.usage.prompt_tokens == 11
        assert service.status()["calls"] == 1

    async def test_pii_never_leaves_the_process(self):
        rec = Recorder(httpx.Response(200, json=completion('{"summary": "ok"}')))
        service = live_service(rec)
        payload = {
            "musteri": "Ayşe Yılmaz",
            "iban": "TR33 0006 1005 1978 6457 8413 26",
            "tckn": "10000000146",
            "telefon": "+90 532 123 45 67",
            "eposta": "ayse@example.com",
        }
        await service.generate(
            task="t", system="s", payload=payload, schema=Answer, names=["Ayşe Yılmaz"]
        )
        wire = json.dumps(rec.requests[0], ensure_ascii=False)
        for secret in ("Ayşe", "8413 26", "10000000146", "532 123", "ayse@example.com"):
            assert secret not in wire
        assert "MUSTERI_1" in wire and "IBAN_…" in wire and "TCKN_1" in wire

    async def test_json_mode_unsupported_falls_back_to_text(self):
        rec = Recorder(
            httpx.Response(400, json={"error": {"message": "response_format is not supported"}}),
            httpx.Response(200, json=completion('Yanıt: {"summary": "düz metin"}')),
        )
        service = live_service(rec)
        result = await service.generate(task="t", system="s", payload={}, schema=Answer)
        assert result.llm_mode == "live" and result.output.summary == "düz metin"
        assert "response_format" not in rec.requests[1]
        assert service.live.supports_json_mode is False  # type: ignore[union-attr]

    async def test_invalid_json_repaired_once(self):
        rec = Recorder(
            httpx.Response(200, json=completion("bu json değil")),
            httpx.Response(200, json=completion('{"summary": "onarıldı"}')),
        )
        result = await live_service(rec).generate(task="t", system="s", payload={}, schema=Answer)
        assert result.llm_mode == "live" and result.output.summary == "onarıldı"
        assert len(rec.requests) == 2
        assert "geçersiz" in rec.requests[1]["messages"][-1]["content"]
        assert result.usage.prompt_tokens == 22

    async def test_invalid_json_twice_falls_back_to_demo(self):
        rec = Recorder(httpx.Response(200, json=completion("hâlâ değil")))
        service = live_service(rec)
        result = await service.generate(
            task="txn_narrative",
            system="s",
            payload={},
            schema=TransactionNarrative,
            demo_context={"risk_score": 0.2, "transaction": {"transaction_id": "T"}},
        )
        assert result.llm_mode == "fallback" and result.llm_error_kind == "invalid_json"
        assert result.output.recommended_action == "pass"
        assert service.status()["failures"] == 1

    @pytest.mark.parametrize(
        ("response", "kind"),
        [
            (httpx.Response(429, json={"error": {"message": "slow down"}}), "rate_limit"),
            (httpx.Response(503, json={"error": {"message": "down"}}), "server_error"),
            (httpx.Response(401, json={"error": {"message": "bad key"}}), "auth"),
        ],
    )
    async def test_http_errors_fall_back(self, response, kind):
        service = live_service(Recorder(response))

        class Smoke(BaseModel):
            status: str

        result = await service.generate(task="smoke", system="s", payload={}, schema=Smoke)
        assert result.llm_mode == "fallback" and result.llm_error_kind == kind
        assert result.output.status == "ok"

    async def test_timeout_falls_back(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout", request=request)

        class Smoke(BaseModel):
            status: str

        result = await live_service(Recorder(boom)).generate(
            task="smoke", system="s", payload={}, schema=Smoke
        )
        assert result.llm_mode == "fallback" and result.llm_error_kind == "timeout"

    async def test_connection_error_falls_back(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        class Smoke(BaseModel):
            status: str

        result = await live_service(Recorder(boom)).generate(
            task="smoke", system="s", payload={}, schema=Smoke
        )
        assert result.llm_error_kind == "connection"

    async def test_schema_and_citation_validation(self):
        rec = Recorder(
            httpx.Response(200, json=completion('{"summary": "x", "evidence": ["TX-UYDURMA"]}'))
        )
        service = live_service(rec)
        # A hallucinated evidence id is rejected (after one repair) and the
        # demo answer is used instead.

        class Cited(BaseModel):
            status: str = "ok"
            message: str = ""
            evidence: list[str] = []

        def validator(obj: Cited) -> list[str]:
            return [f"bilinmeyen kanıt {e}" for e in obj.evidence if e != "TX-1"]

        result = await service.generate(
            task="smoke", system="s", payload={}, schema=Cited, validator=validator
        )
        assert result.llm_mode == "fallback" and result.llm_error_kind == "citation_invalid"
        assert len(rec.requests) == 2  # original + one repair attempt

    async def test_live_stream_and_mid_stream_failure(self):
        def sse(request: httpx.Request) -> httpx.Response:
            chunks = [
                {"choices": [{"index": 0, "delta": {"reasoning_content": "gizli"}}]},
                {"choices": [{"index": 0, "delta": {"content": "Merhaba "}}]},
                {"choices": [{"index": 0, "delta": {"content": "MUSTE"}}]},
                {"choices": [{"index": 0, "delta": {"content": "RI_1"}}]},
            ]
            meta = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m"}
            body = "".join(f"data: {json.dumps({**meta, **c})}\n\n" for c in chunks)
            body += "data: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        service = live_service(Recorder(sse))
        events = [
            e
            async for e in service.stream_text(
                task="chat",
                system="s",
                payload={"musteri": "Ayşe Yılmaz"},
                instruction="i",
                names=["Ayşe Yılmaz"],
            )
        ]
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        assert text == "Merhaba Ayşe Yılmaz" and "gizli" not in text
        assert events[-1]["llm_mode"] == "live"

        failing = live_service(Recorder(httpx.Response(500, json={"error": {"message": "x"}})))
        events = [
            e
            async for e in failing.stream_text(
                task="chat",
                system="s",
                payload={},
                instruction="i",
                demo_context={"question": "q", "facts": ["demo kanıtı"]},
            )
        ]
        assert events[-1]["llm_mode"] == "fallback"
        assert "demo kanıtı" in "".join(e.get("text", "") for e in events)


class TestToolTurn:
    async def test_demo_tool_plan_then_final(self):
        from app.llm.types import ChatRequest

        demo = DeterministicLLM()
        request = ChatRequest(
            messages=[{"role": "user", "content": "x"}],
            task="smoke",
            tools=[{"type": "function", "function": {"name": "get_case"}}],
            context={"tool_plan": [{"name": "get_case", "arguments": {"case_id": 1}}]},
        )
        first = await demo.chat(request)
        assert first.tool_calls[0].name == "get_case"
        request.messages.append({"role": "tool", "content": "{}", "tool_call_id": "x"})
        final = await demo.chat(request)
        assert not final.tool_calls and json.loads(final.content)["status"] == "ok"
