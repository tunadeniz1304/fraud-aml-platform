"""Investigation tools exposed to the copilot (OpenAI function calling).

Each tool is read-only, returns JSON-serialisable data and the **evidence
ids** it surfaced; the union of those ids is what the copilot may cite.

``get_case`` · ``get_customer_profile`` · ``get_recent_transactions`` ·
``get_graph_neighborhood`` · ``sanctions_check`` · ``similar_past_cases`` ·
``get_rule_hits``
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.cases.service import CaseService
from app.core.behavior_store import BehaviorStore
from app.core.sanctions import SanctionScreener
from app.db.database import Database
from app.db.models import Case, Decision, Transaction
from app.features.extractor import CustomerDirectory
from app.graph.entity_graph import EntityGraph

ToolFn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class CaseScope:
    """The case under investigation: tools may only read its own customer's data.

    A prompt-injected or confused model must not be able to pull another
    customer's profile, transactions or cases through the copilot.
    """

    case_id: int
    customer_id: str


def _prop(kind: str, description: str) -> dict[str, str]:
    return {"type": kind, "description": description}


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "get_case": {
        "description": "Vakanın özeti, alert'leri, işlemleri, reason code'ları ve zaman çizelgesi.",
        "parameters": {"case_id": _prop("integer", "Vaka numarası")},
        "required": ["case_id"],
    },
    "get_customer_profile": {
        "description": "Müşterinin KYC bilgisi, adaptif davranış profili ve hesap durumu.",
        "parameters": {"customer_id": _prop("string", "Müşteri numarası")},
        "required": ["customer_id"],
    },
    "get_recent_transactions": {
        "description": "Müşterinin son işlemleri ve kararları.",
        "parameters": {
            "customer_id": _prop("string", "Müşteri numarası"),
            "limit": _prop("integer", "En fazla kaç işlem (1-50)"),
        },
        "required": ["customer_id"],
    },
    "get_graph_neighborhood": {
        "description": "Müşterinin varlık grafındaki komşuluğu: halka, fraud yakınlığı, cihazlar.",
        "parameters": {
            "customer_id": _prop("string", "Müşteri numarası"),
            "hops": _prop("integer", "Kaç adım (1-3)"),
        },
        "required": ["customer_id"],
    },
    "sanctions_check": {
        "description": "Bir ismi yaptırım/PEP listesine karşı tarar (tam + bulanık).",
        "parameters": {"name": _prop("string", "Taranacak ad soyad / unvan")},
        "required": ["name"],
    },
    "similar_past_cases": {
        "description": "Analistçe doğrulanmış benzer geçmiş fraud vakaları (RAG).",
        "parameters": {"case_id": _prop("integer", "Mevcut vaka numarası")},
        "required": ["case_id"],
    },
    "get_rule_hits": {
        "description": "Bir işlemin skor bileşenleri, tetiklenen kurallar ve reason code'ları.",
        "parameters": {"transaction_id": _prop("string", "İşlem numarası")},
        "required": ["transaction_id"],
    },
}


def openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": {
                    "type": "object",
                    "properties": spec["parameters"],
                    "required": spec["required"],
                },
            },
        }
        for name, spec in TOOL_SPECS.items()
    ]


@dataclass
class ToolOutput:
    data: dict[str, Any]
    evidence: list[str] = field(default_factory=list)

    def as_message(self) -> str:
        return json.dumps(
            {**self.data, "evidence_ids": self.evidence}, ensure_ascii=False, default=str
        )


class CopilotTools:
    def __init__(
        self,
        *,
        db: Database,
        cases: CaseService,
        customers: CustomerDirectory,
        sanctions: SanctionScreener,
        graph: EntityGraph | None = None,
        vector: BehaviorStore | None = None,
        account_status: Callable[[str], str | None] | None = None,
        profile_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.db = db
        self.cases = cases
        self.customers = customers
        self.sanctions = sanctions
        self.graph = graph
        self.vector = vector
        self.account_status = account_status or (lambda cid: None)
        self.profile_lookup = profile_lookup or (lambda cid: None)
        self._fns: dict[str, ToolFn] = {
            "get_case": self.get_case,
            "get_customer_profile": self.get_customer_profile,
            "get_recent_transactions": self.get_recent_transactions,
            "get_graph_neighborhood": self.get_graph_neighborhood,
            "sanctions_check": self.sanctions_check,
            "similar_past_cases": self.similar_past_cases,
            "get_rule_hits": self.get_rule_hits,
        }

    @property
    def names(self) -> list[str]:
        return list(self._fns)

    async def call(
        self, name: str, arguments: dict[str, Any], *, scope: CaseScope | None = None
    ) -> ToolOutput:
        fn = self._fns.get(name)
        if fn is None:
            return ToolOutput({"error": f"bilinmeyen araç: {name}"})
        if scope is not None:
            violation = await self._scope_violation(arguments, scope)
            if violation:
                return ToolOutput({"error": violation})
        try:
            data = await fn(**arguments)
        except TypeError as exc:
            return ToolOutput({"error": f"geçersiz argüman: {exc}"})
        except Exception as exc:  # noqa: BLE001 - tool errors go back to the model as data
            return ToolOutput({"error": f"araç hatası: {type(exc).__name__}"})
        evidence = [str(e) for e in data.pop("_evidence", [])]
        return ToolOutput(data, evidence)

    async def _scope_violation(self, arguments: dict[str, Any], scope: CaseScope) -> str | None:
        """Reason the call reaches outside the case, or ``None`` if it is in scope."""
        if "customer_id" in arguments and str(arguments["customer_id"]) != scope.customer_id:
            return "kapsam dışı: yalnızca bu vakanın müşterisi sorgulanabilir"
        if "case_id" in arguments:
            try:
                case_id = int(arguments["case_id"])
            except (TypeError, ValueError):
                return "geçersiz argüman: case_id"
            if case_id != scope.case_id:
                return "kapsam dışı: yalnızca incelenen vaka sorgulanabilir"
        if "transaction_id" in arguments:
            async with self.db.session() as session:
                owner = (
                    await session.execute(
                        select(Transaction.customer_id).where(
                            Transaction.id == str(arguments["transaction_id"])
                        )
                    )
                ).scalar_one_or_none()
            if owner is not None and owner != scope.customer_id:
                return "kapsam dışı: işlem bu vakanın müşterisine ait değil"
        return None

    # --- tools ------------------------------------------------------------------------
    async def get_case(self, case_id: int) -> dict[str, Any]:
        case = await self.cases.get_case(int(case_id))
        evidence = [f"CASE-{case['id']}", case["customer_id"]]
        alerts = []
        for alert in case["alerts"]:
            evidence += [f"ALERT-{alert['id']}", alert["transaction_id"]]
            codes = [r.get("code") for r in alert.get("reason_codes") or []]
            evidence += [c for c in codes if c]
            tx = alert.get("transaction") or {}
            alerts.append(
                {
                    "alert_id": f"ALERT-{alert['id']}",
                    "transaction_id": alert["transaction_id"],
                    "alert_type": alert["alert_type"],
                    "decision": alert["decision"],
                    "risk_score": alert["risk_score"],
                    "amount_try": alert["amount_try"],
                    "ts": str(tx.get("ts", "")),
                    "beneficiary": tx.get("beneficiary_iban") or tx.get("beneficiary_id"),
                    "beneficiary_name": tx.get("beneficiary_name"),
                    "purpose": tx.get("purpose"),
                    "channel": tx.get("channel"),
                    "country": tx.get("country"),
                    "reason_codes": [
                        {"code": r.get("code"), "text": r.get("text")}
                        for r in alert.get("reason_codes") or []
                    ],
                }
            )
        events = []
        for event in case["events"]:
            evidence.append(f"EVT-{event['id']}")
            events.append(
                {
                    "event_id": f"EVT-{event['id']}",
                    "type": event["event_type"],
                    "actor": event["actor"],
                    "at": str(event["created_at"]),
                    "payload": {k: v for k, v in (event["payload"] or {}).items() if k != "fields"},
                }
            )
        if case.get("ring_id"):
            evidence.append(case["ring_id"])
        return {
            "case_id": f"CASE-{case['id']}",
            "customer_id": case["customer_id"],
            "case_type": case["case_type"],
            "title": case["title"],
            "status": case["status"],
            "priority": case["priority"],
            "total_amount_try": case["total_amount_try"],
            "ring_id": case.get("ring_id"),
            "suspicion_at": str(case["suspicion_at"]),
            "masak_deadline": str(case["masak_deadline"]),
            "alerts": alerts,
            "timeline": events,
            "_evidence": evidence,
        }

    async def get_customer_profile(self, customer_id: str) -> dict[str, Any]:
        record = self.customers.raw(customer_id)
        if record is None:
            return {"error": "müşteri bulunamadı"}
        static = self.customers.get(customer_id)
        adaptive = self.profile_lookup(customer_id) or {}
        return {
            "customer_id": customer_id,
            "name": record.get("name", ""),
            "segment": record.get("segment", "bireysel"),
            "home_city": record.get("home_city", ""),
            "birth_year": record.get("birth_year"),
            "vulnerable": bool(record.get("vulnerable")),
            "avg_amount_try": static.avg_amount_try if static else None,
            "known_devices": len(record.get("known_device_ids") or []),
            "typical_hours": record.get("typical_hours"),
            "channels": record.get("known_channels"),
            "account_status": self.account_status(customer_id),
            "adaptive_profile": adaptive,
            "_evidence": [customer_id, f"PROFILE-{customer_id}"],
        }

    async def get_recent_transactions(self, customer_id: str, limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(Transaction, Decision)
                    .outerjoin(Decision, Decision.transaction_id == Transaction.id)
                    .where(Transaction.customer_id == customer_id)
                    .order_by(Transaction.ts.desc())
                    .limit(limit)
                )
            ).all()
        items = [
            {
                "transaction_id": tx.id,
                "ts": str(tx.ts),
                "amount_try": float(tx.amount_try),
                "beneficiary": tx.beneficiary_iban or tx.beneficiary_id,
                "purpose": tx.purpose,
                "decision": decision.decision if decision else None,
                "risk_score": decision.risk_score if decision else None,
            }
            for tx, decision in rows
        ]
        return {
            "customer_id": customer_id,
            "transactions": items,
            "_evidence": [i["transaction_id"] for i in items],
        }

    async def get_graph_neighborhood(self, customer_id: str, hops: int = 2) -> dict[str, Any]:
        if self.graph is None:
            return {"error": "graf devre dışı"}
        elements = self.graph.neighbourhood(customer_id, hops=max(1, min(int(hops), 3)))
        nodes = [n["data"] for n in elements["nodes"]]
        ring = self.graph.ring_id(customer_id)
        evidence = [f"GRAPH-{customer_id}"] + ([ring] if ring else [])
        return {
            "customer_id": customer_id,
            "ring_id": ring,
            "nodes": len(nodes),
            "edges": len(elements["edges"]),
            "fraud_nodes": [n["label"] for n in nodes if n.get("fraud")][:10],
            "shared_devices": [n["label"] for n in nodes if n["type"] == "device"][:10],
            "counterparties": [n["label"] for n in nodes if n["type"] == "customer"][:15],
            "_evidence": evidence,
        }

    async def sanctions_check(self, name: str) -> dict[str, Any]:
        hits = self.sanctions.screen(name)
        return {
            "query": name,
            "hits": [
                {
                    "id": h.get("id"),
                    "name": h.get("name"),
                    "program": h.get("program"),
                    "match_type": h.get("match_type"),
                    "score": h.get("match_score"),
                }
                for h in hits
            ],
            "_evidence": ["SANCTIONS-CHECK"] + [str(h.get("id")) for h in hits if h.get("id")],
        }

    async def similar_past_cases(self, case_id: int) -> dict[str, Any]:
        case = await self.cases.get_case(int(case_id))
        codes = {r.get("code") for a in case["alerts"] for r in a.get("reason_codes") or []}
        found: list[dict[str, Any]] = []
        if self.vector is not None and self.vector.enabled:
            query = f"{case['case_type']} " + " ".join(sorted(c for c in codes if c))
            found = [
                {
                    "case_id": f"CASE-{h['case_id']}",
                    "summary": h["document"][:300],
                    "distance": round(float(h["distance"]), 4),
                }
                for h in self.vector.similar_cases(query)
                if str(h["case_id"]) != str(case_id)
            ]
        if not found:  # DB fallback: closed FRAUD cases of the same typology
            async with self.db.session() as session:
                rows = (
                    await session.execute(
                        select(Case)
                        .where(
                            Case.decision == "FRAUD",
                            Case.case_type == case["case_type"],
                            Case.id != int(case_id),
                        )
                        .order_by(Case.closed_at.desc())
                        .limit(3)
                    )
                ).scalars()
                found = [
                    {
                        "case_id": f"CASE-{c.id}",
                        "summary": c.title,
                        "total_amount_try": float(c.total_amount_try or 0),
                    }
                    for c in rows
                ]
        return {
            "case_id": f"CASE-{case_id}",
            "similar": found,
            "_evidence": [f["case_id"] for f in found],
        }

    async def get_rule_hits(self, transaction_id: str) -> dict[str, Any]:
        async with self.db.session() as session:
            decision = (
                await session.execute(
                    select(Decision).where(Decision.transaction_id == transaction_id)
                )
            ).scalar_one_or_none()
        if decision is None:
            return {"error": "karar bulunamadı"}
        codes = [r.get("code") for r in decision.reason_codes or []]
        return {
            "transaction_id": transaction_id,
            "decision": decision.decision,
            "risk_score": decision.risk_score,
            "components": decision.components,
            "reason_codes": decision.reason_codes,
            "rule_version": decision.rule_version,
            "model_version": decision.model_version,
            "_evidence": [transaction_id, *(c for c in codes if c)],
        }
