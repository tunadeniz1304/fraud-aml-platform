"""Case management (P0.6): grouping, SLA, decisions → labels, maker-checker."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.cases import sla
from app.cases.service import (
    CaseError,
    CaseNotFoundError,
    CaseService,
    MakerCheckerError,
    alert_type,
    sib_draft_hash,
)
from app.db.models import Case, Decision, Label


def event(tx_id: str, customer: str = "CUST-0001", **extra) -> dict:
    return {
        "transaction_id": tx_id,
        "customer_id": customer,
        "decision": "HOLD",
        "risk_score": 0.7,
        "amount_try": 10_000,
        "reason_codes": [{"code": "R_X", "text": "x", "source": "rule", "weight": 0.5}],
        "rule_hits": [],
        **extra,
    }


@pytest.fixture()
async def service(store) -> CaseService:
    return CaseService(store.db, accounts=store.accounts, writer=store.writer)


async def add_decision(store, tx_id: str, customer: str = "CUST-0001", status="BEKLEMEDE"):
    async with store.db.transaction() as session:
        session.add(
            Decision(
                transaction_id=tx_id,
                customer_id=customer,
                decision="HOLD",
                risk_score=0.7,
                status=status,
            )
        )


class TestSla:
    def test_business_days_skip_weekends_and_holidays(self):
        friday = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
        assert sla.add_business_days(friday, 10).date().isoformat() == "2026-10-09"
        monday = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 29 Ekim resmi tatil
        assert sla.add_business_days(monday, 3).date().isoformat() == "2026-10-30"
        assert sla.business_days_between(friday, friday) == 0
        assert sla.business_days_between(friday, datetime(2026, 10, 9, tzinfo=UTC)) == 10
        assert not sla.is_business_day(datetime(2026, 10, 29).date())
        assert sla.is_business_day(datetime(2026, 10, 30).date(), extra=[])
        assert sla.masak_deadline(friday) > friday
        assert sla.internal_sla_due(friday) == friday + timedelta(hours=4)


class TestAlertIntake:
    def test_alert_type_priority(self):
        assert alert_type({"sanctions_hit": True}) == "YAPTIRIM"
        assert alert_type({"unknown_customer": True}) == "BILINMEYEN_MUSTERI"
        hits = [{"tags": ["ato"]}, {"tags": ["aml", "network"]}]
        assert alert_type({"rule_hits": hits}) == "AML"
        assert alert_type({"rule_hits": [{"tags": ["card"]}]}) == "CARD_TESTING"
        assert alert_type({}) == "DAVRANIS"

    async def test_only_hold_block_or_case_required_create_alerts(self, service):
        assert await service.on_decision(event("T0", decision="ALLOW")) is None
        assert await service.on_decision(event("T1", decision="STEP_UP")) is None
        assert (
            await service.on_decision(event("T2", decision="BLOCK", account_blocked=True)) is None
        )
        assert await service.on_decision(event("T3", decision="ALLOW", case_required=True))

    async def test_grouping_priority_and_type_upgrade(self, service):
        first = await service.on_decision(event("T1"))
        second = await service.on_decision(event("T2", risk_score=0.9, sanctions_hit=True))
        other = await service.on_decision(event("T3", customer="CUST-0002", decision="BLOCK"))
        assert first == second and other != first
        case = await service.get_case(first)
        assert case["alert_count"] == 2 and case["case_type"] == "YAPTIRIM"
        assert case["priority"] == pytest.approx(0.7 * 10_000 + 0.9 * 10_000)
        assert case["total_amount_try"] == 20_000
        assert case["title"].startswith("Yaptırım") and case["status"] == "YENI"
        assert [e["event_type"] for e in case["events"]] == ["OPENED", "ALERT_ADDED", "ALERT_ADDED"]
        assert case["masak_deadline"] > case["suspicion_at"]
        assert case["alerts"][1]["severity"] == "critical"
        listing = await service.list_cases(order="priority")
        assert [c["id"] for c in listing] == [first, other]
        alerts = await service.list_alerts()
        assert {a["transaction_id"] for a in alerts} == {"T1", "T2", "T3"}

    async def test_a11_replayed_intake_creates_one_alert(self, service, store):
        """An outbox replay / redelivery of the same decision is a no-op."""
        first = await service.on_decision(event("DUP-1"))
        assert first is not None
        assert await service.on_decision(event("DUP-1")) is None
        other = CaseService(store.db, accounts=store.accounts, writer=store.writer)
        assert await other.on_decision(event("DUP-1")) is None  # another worker
        case = await service.get_case(first)
        assert case["alert_count"] == 1 and case["total_amount_try"] == 10_000
        assert case["priority"] == pytest.approx(0.7 * 10_000)
        assert [a["transaction_id"] for a in await service.list_alerts()] == ["DUP-1"]

    async def test_a11_concurrent_intake_is_rolled_back_by_the_unique_index(
        self, service, store, monkeypatch
    ):
        """The in-transaction check can race on another worker; the unique
        ``alerts.transaction_id`` index is the backstop and the loser is a no-op."""
        first = await service.on_decision(event("DUP-2"))
        other = CaseService(store.db, accounts=store.accounts, writer=store.writer)
        real_execute = type(other)._intake

        async def _skip_check(self, ev):  # the racing worker saw no alert yet
            from sqlalchemy.ext.asyncio import AsyncSession

            orig = AsyncSession.execute
            calls = {"n": 0}

            async def execute(session, stmt, *a, **kw):
                calls["n"] += 1
                if calls["n"] == 1:  # the duplicate probe
                    return await orig(session, select(Case.id).where(Case.id == -1), *a, **kw)
                return await orig(session, stmt, *a, **kw)

            monkeypatch.setattr(AsyncSession, "execute", execute)
            try:
                return await real_execute(self, ev)
            finally:
                monkeypatch.setattr(AsyncSession, "execute", orig)

        monkeypatch.setattr(CaseService, "_intake", _skip_check)
        assert await other.on_decision(event("DUP-2")) is None
        case = await service.get_case(first)
        assert case["alert_count"] == 1

    async def test_a11_case_clock_starts_at_the_decision(self, service):
        decided = datetime.now(UTC) - timedelta(days=2)
        case_id = await service.on_decision(event("LATE-1", decided_at=decided.isoformat()))
        case = await service.get_case(case_id)
        assert abs((case["suspicion_at"] - decided).total_seconds()) < 1
        assert abs((case["masak_deadline"] - sla.masak_deadline(decided)).total_seconds()) < 1
        assert abs((case["internal_sla_due"] - sla.internal_sla_due(decided)).total_seconds()) < 1
        # a decided_at in the future (clock skew) never pushes the deadline out
        future = datetime.now(UTC) + timedelta(days=30)
        other = await service.on_decision(
            event("LATE-2", customer="CUST-0009", decided_at=future.isoformat())
        )
        assert (await service.get_case(other))["suspicion_at"] <= datetime.now(UTC)

    async def test_ring_grouping_and_window_expiry(self, service, store):
        a = await service.on_decision(event("R1", customer="CUST-0003", ring_id="RING-7"))
        b = await service.on_decision(event("R2", customer="CUST-0004", ring_id="RING-7"))
        assert a == b
        async with store.db.transaction() as session:
            await session.execute(
                update(Case).values(updated_at=datetime.now(UTC) - timedelta(days=3))
            )
        c = await service.on_decision(event("R3", customer="CUST-0003", ring_id="RING-7"))
        assert c != a

    async def test_list_filters_sla_flags_and_counts(self, service, store):
        case_id = await service.on_decision(event("S1"))
        async with store.db.transaction() as session:
            await session.execute(
                update(Case).values(internal_sla_due=datetime.now(UTC) - timedelta(minutes=5))
            )
        [row] = await service.list_cases(status="OPEN", order="sla")
        assert row["id"] == case_id and row["internal_sla_breached"] is True
        assert await service.list_cases(status="KAPANDI_FRAUD") == []
        assert await service.list_cases(customer_id="CUST-0001", case_type="DAVRANIS")
        assert await service.list_cases(assigned_to="nobody") == []
        scan = await service.sla_scan()
        assert scan == {
            "open": 1,
            "internal_sla_breached": 1,
            "masak_due_soon": 0,
            "masak_overdue": 0,
        }
        # L5: a passed MASAK deadline counts as overdue, not as "due soon"
        async with store.db.transaction() as session:
            await session.execute(
                update(Case).values(masak_deadline=datetime.now(UTC) - timedelta(days=1))
            )
        scan = await service.sla_scan()
        assert scan["masak_overdue"] == 1 and scan["masak_due_soon"] == 0
        assert await service.counts() == {"YENI": 1}


class TestAnalystActions:
    async def test_assign_status_notes_evidence(self, service):
        case_id = await service.on_decision(event("A1"))
        case = await service.assign(case_id, "analist", "analist")
        assert case["status"] == "INCELENIYOR" and case["assigned_to"] == "analist"
        with pytest.raises(CaseError):
            await service.set_status(case_id, "YENI", "analist")
        with pytest.raises(CaseError, match="karar verin"):
            await service.set_status(case_id, "KAPANDI_FRAUD", "analist")
        case = await service.set_status(case_id, "BEKLEMEDE", "analist", "müşteri aranacak")
        assert case["status"] == "BEKLEMEDE"
        note = await service.add_note(case_id, " Müşteri işlemi tanımadı. ", "analist")
        assert note["text"] == "Müşteri işlemi tanımadı."
        with pytest.raises(CaseError):
            await service.add_note(case_id, "   ", "analist")
        content = base64.b64encode(b"ekran goruntusu").decode()
        ev = await service.add_evidence(
            case_id,
            filename="kanit.png",
            content_type="image/png",
            content_b64=content,
            note="SMS",
            actor="analist",
        )
        assert ev["size"] == 15 and len(ev["sha256"]) == 64
        stored = await service.evidence(case_id, ev["id"])
        assert base64.b64decode(stored["content_b64"]) == b"ekran goruntusu"
        detail = await service.get_case(case_id)
        evidence_event = next(e for e in detail["events"] if e["event_type"] == "EVIDENCE")
        assert "content_b64" not in evidence_event["payload"]
        with pytest.raises(CaseError):
            await service.add_evidence(
                case_id, filename="x", content_type="x", content_b64="%%%", note="", actor="a"
            )
        with pytest.raises(CaseNotFoundError):
            await service.evidence(case_id, 999_999)
        with pytest.raises(CaseNotFoundError):
            await service.get_case(424242)

    async def test_evidence_size_limit(self, service, monkeypatch):
        from app.config import get_settings

        case_id = await service.on_decision(event("A2"))
        monkeypatch.setattr(get_settings(), "evidence_max_bytes", 4)
        with pytest.raises(CaseError, match="büyük"):
            await service.add_evidence(
                case_id,
                filename="b.bin",
                content_type="x",
                content_b64=base64.b64encode(b"12345").decode(),
                note="",
                actor="a",
            )

    async def test_fraud_decision_labels_rejects_holds_and_blocks(self, service, store):
        case_id = await service.on_decision(event("D1"))
        await service.on_decision(event("D2"))
        await add_decision(store, "D1")
        await add_decision(store, "D2")
        case = await service.decide(case_id, "FRAUD", "analist", "müşteri teyit etti")
        assert case["status"] == "KAPANDI_FRAUD" and case["decision"] == "FRAUD"
        assert case["closed_at"] is not None
        async with store.db.session() as session:
            labels = (await session.execute(select(Label))).scalars().all()
            statuses = set((await session.execute(select(Decision.status))).scalars())
        assert {(lb.transaction_id, lb.label, lb.source) for lb in labels} == {
            ("D1", 1, "analyst"),
            ("D2", 1, "analyst"),
        }
        assert statuses == {"REDDEDILDI"}
        assert store.get_status("CUST-0001") == "BLOKE"
        with pytest.raises(CaseError, match="kapalı"):
            await service.decide(case_id, "TEMIZ", "analist")
        with pytest.raises(CaseError):
            await service.assign(case_id, "x", "x")
        reopened = await service.set_status(case_id, "INCELENIYOR", "kidemli", "yeniden")
        assert reopened["decision"] is None and reopened["closed_at"] is None
        with pytest.raises(CaseError):
            await service.decide(case_id, "BELKI", "analist")

    async def test_clean_decision_releases_hold_and_review_status(self, service, store):
        await store.accounts.escalate("CUST-0002", "INCELENIYOR", reason="hold")
        case_id = await service.on_decision(event("C1", customer="CUST-0002"))
        await add_decision(store, "C1", "CUST-0002")
        await service.decide(case_id, "TEMIZ", "analist")
        async with store.db.session() as session:
            label = (await session.execute(select(Label.label))).scalar_one()
            status = (await session.execute(select(Decision.status))).scalar_one()
        assert label == 0 and status == "SERBEST"
        assert store.get_status("CUST-0002") == "AKTIF"


class TestMakerChecker:
    async def test_unblock_requires_a_different_approver(self, service, store):
        await store.set_status("CUST-0001", "BLOKE")
        request = await service.request_approval(
            "UNBLOCK", "CUST-0001", {"to_status": "AKTIF"}, "analist", "müşteri doğrulandı"
        )
        assert request["status"] == "BEKLIYOR"
        with pytest.raises(CaseError, match="bekleyen"):
            await service.request_approval("UNBLOCK", "CUST-0001", {}, "analist")
        with pytest.raises(MakerCheckerError):
            await service.decide_approval(request["id"], approve=True, actor="analist")
        assert store.get_status("CUST-0001") == "BLOKE"
        done = await service.decide_approval(request["id"], approve=True, actor="kidemli")
        assert done["status"] == "ONAYLANDI" and done["result"]["hesap_durumu"] == "AKTIF"
        assert store.get_status("CUST-0001") == "AKTIF"
        with pytest.raises(CaseError, match="sonuçlandı"):
            await service.decide_approval(request["id"], approve=False, actor="kidemli")
        assert await service.list_approvals() == []
        assert len(await service.list_approvals(None)) == 1
        with pytest.raises(CaseNotFoundError):
            await service.decide_approval(99, approve=True, actor="x")
        with pytest.raises(CaseError):
            await service.request_approval("DELETE_ALL", "x", {}, "a")

    async def test_rejected_unblock_keeps_block(self, service, store):
        await store.set_status("CUST-0003", "BLOKE")
        req = await service.request_approval("UNBLOCK", "CUST-0003", {}, "analist")
        out = await service.decide_approval(req["id"], approve=False, actor="kidemli", note="hayır")
        assert out["status"] == "REDDEDILDI" and out["result"] == {}
        assert store.get_status("CUST-0003") == "BLOKE"

    async def test_sib_flow(self, service):
        case_id = await service.on_decision(event("SIB1"))
        with pytest.raises(CaseError, match="taslağı"):
            await service.request_approval("SIB", str(case_id), {}, "analist")
        await service.save_sib_draft(case_id, {"supheli": "MUSTERI_1"}, "analist")
        with pytest.raises(CaseError, match="FRAUD"):
            await service.request_approval("SIB", str(case_id), {}, "analist")
        await service.decide(case_id, "FRAUD", "analist")
        req = await service.request_approval("SIB", str(case_id), {}, "analist")
        assert (await service.get_case(case_id))["sib_status"] == "SIB_ONAY_BEKLIYOR"
        rejected = await service.decide_approval(req["id"], approve=False, actor="kidemli")
        assert rejected["status"] == "REDDEDILDI"
        assert (await service.get_case(case_id))["sib_status"] == "SIB_TASLAK"
        req = await service.request_approval("SIB", str(case_id), {}, "analist")
        done = await service.decide_approval(req["id"], approve=True, actor="kidemli")
        assert done["result"]["masak_reference"].startswith("SIB-")
        case = await service.get_case(case_id)
        assert case["status"] == "SIB_GONDERILDI" and case["sib_status"] == "SIB_ONAYLANDI"
        assert case["sib_draft"]["masak_reference"] == done["result"]["masak_reference"]
        assert {a["kind"] for a in case["approvals"]} == {"SIB"}
        with pytest.raises(CaseError, match="onaylanmış"):
            await service.save_sib_draft(case_id, {"x": 1}, "analist")

    async def test_sib_draft_is_frozen_and_pinned_while_awaiting_approval(self, service, store):
        """A6: the approver signs off exactly the text that is submitted."""
        case_id = await service.on_decision(event("SIB2"))
        await service.save_sib_draft(case_id, {"supheli": "MUSTERI_1"}, "analist")
        await service.decide(case_id, "FRAUD", "analist")
        req = await service.request_approval("SIB", str(case_id), {}, "analist")
        assert req["payload"]["draft_sha256"] == sib_draft_hash({"supheli": "MUSTERI_1"})
        with pytest.raises(CaseError, match="onay beklerken"):
            await service.save_sib_draft(case_id, {"supheli": "BASKA"}, "analist")
        assert (await service.get_case(case_id))["sib_draft"] == {"supheli": "MUSTERI_1"}
        # a draft changed behind the API's back is refused and nothing is submitted
        async with store.db.transaction() as session:
            await session.execute(
                update(Case).where(Case.id == case_id).values(sib_draft={"supheli": "BASKA"})
            )
        with pytest.raises(CaseError, match="değişti"):
            await service.decide_approval(req["id"], approve=True, actor="kidemli")
        case = await service.get_case(case_id)
        assert case["sib_status"] == "SIB_ONAY_BEKLIYOR" and case["status"] != "SIB_GONDERILDI"
        # reject → editable again → a fresh request pins the new text
        await service.decide_approval(req["id"], approve=False, actor="kidemli")
        await service.save_sib_draft(case_id, {"supheli": "SON"}, "analist")
        req = await service.request_approval("SIB", str(case_id), {}, "analist")
        done = await service.decide_approval(req["id"], approve=True, actor="kidemli")
        assert done["result"]["masak_reference"].startswith("SIB-")
        assert (await service.get_case(case_id))["sib_draft"]["supheli"] == "SON"
