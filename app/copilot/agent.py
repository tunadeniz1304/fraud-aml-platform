"""LLM investigator copilot (P1.5) — Feedzai Farol / NICE InvestigateAI style.

1. **Investigate** — a tool-use loop (OpenAI-compatible function calling; if
   the endpoint does not support tools, a ReAct text protocol) gathers
   evidence with read-only tools, at most ``MAX_STEPS`` steps, every step
   traced. A live failure switches the loop to the deterministic demo model
   (``llm_mode = "fallback"``) — the analyst always gets an answer.
2. **Produce** — structured, pydantic-validated outputs whose every citation
   must exist in the gathered evidence (hallucination guard):
   case summary (5 cited bullets), recommended decision (advisory only — no
   action is taken without an analyst), MASAK ŞİB draft, analyst chat (SSE).

The copilot never changes a score or a decision (§6); KVKK redaction is
applied to everything sent to a live model (``LLMService``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.copilot import templates  # noqa: F401  (registers demo renderers)
from app.copilot.schemas import (
    CaseSummary,
    Recommendation,
    SibDraft,
    TriageLabel,
    citation_validator,
)
from app.copilot.tools import CaseScope, CopilotTools, ToolOutput, openai_tools
from app.llm.redaction import Redactor
from app.llm.service import UNTRUSTED_NOTE, LLMService, untrusted_block
from app.llm.types import (
    ChatRequest,
    ChatResponse,
    ErrorKind,
    LLMCallError,
    LLMResult,
    Usage,
)

logger = logging.getLogger("fraud.copilot")

MAX_STEPS = 6

INVESTIGATE_SYSTEM = (
    "Sen bir bankanın fraud soruşturma copilot'usun. Vakayı araçlarla incele: önce "
    "get_case, sonra müşteri profili, son işlemler, graf komşuluğu, gerekirse yaptırım "
    "taraması ve benzer geçmiş vakalar. En fazla 6 araç çağrısı yap. Karar verme; yalnızca "
    "kanıt topla. Araçlardan gelmeyen bilgi uydurma. Araçlar yalnızca bu vakanın "
    "müşterisi ve işlemleri için çalışır.\n" + UNTRUSTED_NOTE
)
REACT_SYSTEM = (
    INVESTIGATE_SYSTEM
    + "\n\nAraç çağırmak için YALNIZCA şu satırı yaz:\nARAÇ: <araç_adı> <JSON argümanlar>\n"
    "İnceleme bittiğinde yalnızca 'BİTTİ' yaz. Kullanılabilir araçlar:\n"
)
SUMMARY_SYSTEM = (
    "Fraud vaka özeti yaz: tam 5 madde, Türkçe, her madde yalnızca verideki kanıt "
    "kimliklerine (citations) atıf yapsın. Veride olmayan kimlik kullanma."
)
RECOMMEND_SYSTEM = (
    "Vakaya bir karar ÖNER (FRAUD | TEMIZ | INCELEMEYE_DEVAM), güven (0-1), gerekçe ve "
    "kanıt atıfları ver. Bu yalnızca öneridir; aksiyon analist onayı olmadan uygulanmaz."
)
SIB_SYSTEM = (
    "MASAK Şüpheli İşlem Bildirimi (ŞİB) taslağı hazırla: kim, ne, ne zaman, nerede, neden; "
    "şüpheli işlem tipi, işlem listesi ve tutarlar. Tipping-off yasağı uyarısını ekle. "
    "Yalnızca verideki bilgileri ve kanıt kimliklerini kullan."
)
CHAT_SYSTEM = (
    "Fraud analistinin vakayla ilgili sorusunu yalnızca verilen kanıtlara dayanarak, Türkçe "
    "ve kısa yanıtla. Kanıt kimliklerini parantez içinde belirt. Bilmiyorsan söyle."
)
_REACT_RE = re.compile(r"ARA[ÇC]:\s*(\w+)\s*(\{.*\})?", re.IGNORECASE | re.DOTALL)


@dataclass
class Investigation:
    case_id: int
    evidence: dict[str, Any] = field(default_factory=dict)
    evidence_ids: set[str] = field(default_factory=set)
    trace: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "demo"
    error_kind: ErrorKind | None = None
    protocol: str = "tools"
    names: list[str] = field(default_factory=list)
    #: the case's own customer; every tool call is confined to it
    customer_id: str = ""

    def add(self, name: str, arguments: dict[str, Any], out: ToolOutput, ms: float) -> None:
        key = name if name not in self.evidence else f"{name}:{len(self.evidence)}"
        self.evidence[key] = out.data
        self.evidence_ids.update(out.evidence)
        self.trace.append(
            {
                "step": len(self.trace) + 1,
                "tool": name,
                "arguments": arguments,
                "evidence_ids": out.evidence[:20],
                "error": out.data.get("error"),
                "ms": round(ms, 1),
            }
        )

    @property
    def case(self) -> dict[str, Any]:
        return dict(self.evidence.get("get_case") or {})

    def payload(self) -> dict[str, Any]:
        return {**self.evidence, "allowed_citations": sorted(self.evidence_ids)}


def default_plan(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool plan of the deterministic demo model (and a sensible live default)."""
    cid = case["customer_id"]
    plan = [
        {"name": "get_case", "arguments": {"case_id": case["id"]}},
        {"name": "get_customer_profile", "arguments": {"customer_id": cid}},
        {"name": "get_recent_transactions", "arguments": {"customer_id": cid, "limit": 10}},
        {"name": "get_graph_neighborhood", "arguments": {"customer_id": cid, "hops": 2}},
        {"name": "similar_past_cases", "arguments": {"case_id": case["id"]}},
    ]
    if case["case_type"] == "YAPTIRIM":
        names = [
            (a.get("transaction") or {}).get("beneficiary_name") for a in case.get("alerts", [])
        ]
        plan.append(
            {"name": "sanctions_check", "arguments": {"name": next(filter(None, names), cid)}}
        )
    return plan[:MAX_STEPS]


class CopilotAgent:
    def __init__(self, llm: LLMService, tools: CopilotTools) -> None:
        self.llm = llm
        self.tools = tools

    def _names(self, case: dict[str, Any]) -> list[str]:
        customer = self.tools.customers.get(case["customer_id"])
        names = [customer.name] if customer and customer.name else []
        return names + [
            str((a.get("transaction") or {}).get("beneficiary_name") or "")
            for a in case.get("alerts", [])
            if (a.get("transaction") or {}).get("beneficiary_name")
        ]

    # --- evidence gathering -----------------------------------------------------------
    async def investigate(self, case_id: int) -> Investigation:
        case = await self.tools.cases.get_case(case_id)  # raises CaseNotFoundError
        names = self._names(case)
        inv = Investigation(
            case_id=case_id, mode=self.llm.mode, names=names, customer_id=case["customer_id"]
        )
        plan = default_plan(case)
        started = time.perf_counter()
        try:
            await self._tool_loop(inv, case, plan, names, force_demo=False)
        except LLMCallError as exc:
            if exc.kind == "tools_unsupported":
                inv.protocol = "react"
                try:
                    await self._react_loop(inv, case, names)
                except (LLMCallError, TimeoutError) as inner:
                    await self._fallback(inv, case, plan, names, _kind(inner))
            else:
                await self._fallback(inv, case, plan, names, exc.kind)
        except TimeoutError:
            await self._fallback(inv, case, plan, names, "timeout")
        if "get_case" not in inv.evidence:  # the dossier baseline is always present
            await self._run_tool(inv, "get_case", {"case_id": case_id})
        self.llm.note_call("copilot_investigate", inv.mode, started, Usage())  # type: ignore[arg-type]
        return inv

    async def _fallback(
        self,
        inv: Investigation,
        case: dict[str, Any],
        plan: list[dict[str, Any]],
        names: list[str],
        kind: ErrorKind,
    ) -> None:
        logger.warning("[Copilot] canlı araç döngüsü başarısız (%s) — demo planına düşüldü", kind)
        self.llm.note_failure("copilot_investigate", kind)
        inv.mode, inv.error_kind = "fallback", kind
        inv.evidence.clear()
        inv.evidence_ids.clear()
        await self._tool_loop(inv, case, plan, names, force_demo=True)

    async def _run_tool(
        self, inv: Investigation, name: str, arguments: dict[str, Any]
    ) -> ToolOutput:
        t0 = time.perf_counter()
        scope = CaseScope(case_id=inv.case_id, customer_id=inv.customer_id)
        out = await self.tools.call(name, arguments, scope=scope)
        inv.add(name, arguments, out, (time.perf_counter() - t0) * 1000)
        return out

    async def _tool_loop(
        self,
        inv: Investigation,
        case: dict[str, Any],
        plan: list[dict[str, Any]],
        names: list[str],
        *,
        force_demo: bool,
    ) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": INVESTIGATE_SYSTEM},
            {
                "role": "user",
                "content": f"Vaka CASE-{case['id']} ({case['case_type']}, müşteri "
                f"{case['customer_id']}) için kanıt topla.",
            },
        ]
        redactor = Redactor(names)
        for _ in range(MAX_STEPS):
            request = ChatRequest(
                messages=messages,
                task="copilot_investigate",
                context={"tool_plan": plan},
                tools=openai_tools(),
            )
            resp, mode = await self.llm.chat_turn(request, redactor=redactor, force_demo=force_demo)
            if not force_demo:
                inv.mode = mode
            if not resp.tool_calls:
                break
            messages.append(_assistant_message(resp))
            for call in resp.tool_calls:
                out = await self._run_tool(inv, call.name, call.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": untrusted_block(out.as_message()),
                    }
                )

    async def _react_loop(self, inv: Investigation, case: dict[str, Any], names: list[str]) -> None:
        catalog = "\n".join(
            f"- {t['function']['name']}: {t['function']['description']}" for t in openai_tools()
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": REACT_SYSTEM + catalog},
            {"role": "user", "content": f"Vaka CASE-{case['id']} (müşteri {case['customer_id']})."},
        ]
        redactor = Redactor(names)
        for _ in range(MAX_STEPS):
            resp, mode = await self.llm.chat_turn(
                ChatRequest(messages=messages, task="copilot_investigate"), redactor=redactor
            )
            inv.mode = mode
            match = _REACT_RE.search(resp.content or "")
            if not match:
                break
            name = match.group(1)
            try:
                arguments = json.loads(match.group(2) or "{}")
            except json.JSONDecodeError:
                arguments = {}
            out = await self._run_tool(inv, name, arguments)
            messages += [
                {"role": "assistant", "content": resp.content},
                {
                    "role": "user",
                    "content": f"GÖZLEM ({name}):\n{untrusted_block(out.as_message()[:6000])}",
                },
            ]

    # --- structured outputs -----------------------------------------------------------------
    async def _generate(
        self, inv: Investigation, task: str, system: str, schema: Any, instruction: str
    ) -> LLMResult[Any]:
        payload = inv.payload()
        result = await self.llm.generate(
            task=task,
            system=system,
            payload=payload,
            schema=schema,
            instruction=instruction,
            names=inv.names,
            validator=citation_validator(inv.evidence_ids),
            demo_context=payload,
        )
        result.trace = inv.trace
        return result

    async def summarize(self, case_id: int) -> tuple[LLMResult[CaseSummary], Investigation]:
        inv = await self.investigate(case_id)
        result = await self._generate(
            inv,
            "copilot_summary",
            SUMMARY_SYSTEM,
            CaseSummary,
            "Vakayı 5 maddede özetle; her madde kanıt kimliklerine atıf yapsın.",
        )
        return result, inv

    async def recommend(self, case_id: int) -> tuple[LLMResult[Recommendation], Investigation]:
        inv = await self.investigate(case_id)
        result = await self._generate(
            inv,
            "copilot_recommendation",
            RECOMMEND_SYSTEM,
            Recommendation,
            "Karar önerisi, güven, gerekçe ve atıfları ver.",
        )
        return result, inv

    async def sib_draft(self, case_id: int) -> tuple[LLMResult[SibDraft], Investigation]:
        inv = await self.investigate(case_id)
        result = await self._generate(
            inv,
            "sib_draft",
            SIB_SYSTEM,
            SibDraft,
            "MASAK ŞİB taslağını şemaya uygun doldur.",
        )
        return result, inv

    async def chat(self, case_id: int, question: str) -> AsyncIterator[dict[str, Any]]:
        inv = await self.investigate(case_id)
        facts = facts_from(inv)
        payload = {"question": question, "evidence": inv.payload()}
        yield {"type": "trace", "trace": inv.trace, "tool_loop_mode": inv.mode}
        async for event in self.llm.stream_text(
            task="chat",
            system=CHAT_SYSTEM,
            payload=payload,
            instruction=f"Analistin sorusu: {question}",
            names=inv.names,
            demo_context={"question": question, "facts": facts},
        ):
            yield event

    async def triage_text(self, text: str) -> LLMResult[TriageLabel]:
        """Async APP text classification (case enrichment only, never the score)."""
        return await self.llm.generate(
            task="app_text_triage",
            system="Ödeme açıklamasını APP dolandırıcılığı kalıbına göre sınıflandır.",
            payload={"text": text},
            schema=TriageLabel,
            instruction="Etiket (guvenli_hesap|yatirim|sahte_yetkili|romantik|diger|yok) ve güven.",
        )


def facts_from(inv: Investigation) -> list[str]:
    case = inv.case
    facts: list[str] = []
    if case:
        facts.append(
            f"{case['case_id']}: {case['title']} — {len(case['alerts'])} alert, toplam "
            f"{case['total_amount_try']:,.0f} TL".replace(",", ".")
        )
        for alert in case["alerts"][:2]:
            reasons = ", ".join(r["text"] for r in alert["reason_codes"][:2] if r.get("text"))
            facts.append(f"{alert['transaction_id']} ({alert['decision']}): {reasons}")
    graph = inv.evidence.get("get_graph_neighborhood") or {}
    if graph.get("ring_id"):
        facts.append(f"Müşteri {graph['ring_id']} halkasında ({graph.get('nodes')} düğüm)")
    profile = inv.evidence.get("get_customer_profile") or {}
    if profile.get("account_status"):
        facts.append(f"Hesap durumu: {profile['account_status']}")
    return facts


def _assistant_message(resp: ChatResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": resp.content or "",
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": json.dumps(c.arguments, ensure_ascii=False),
                },
            }
            for c in resp.tool_calls
        ],
    }


def _kind(exc: BaseException) -> ErrorKind:
    return exc.kind if isinstance(exc, LLMCallError) else "timeout"
