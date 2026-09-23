"""DeviceMuleGraph için odaklı testler (sıfır-yanlış-pozitif puanlama).

Tek bir farklı müşteri -> 0 (NORMAL); eşik aşılınca puan yükselir. Deterministik
zaman damgaları kullanılır; pencere dışına düşen olaylar puanı düşürür.
"""

from __future__ import annotations

import pytest

from app.core.mule_graph import DeviceMuleGraph, mule_explain


def _tx(customer_id: str, device_id: str, beneficiary_id: str) -> dict:
    return {
        "transaction_id": "TX",
        "customer_id": customer_id,
        "device_id": device_id,
        "beneficiary_id": beneficiary_id,
    }


def test_single_customer_is_zero() -> None:
    """Tek müşteri/cihaz/alıcı NORMAL -> tüm sinyaller 0 (yanlış pozitif yok)."""
    graph = DeviceMuleGraph()
    scores = graph.update(_tx("CUST-1", "DEV-A", "BEN-1"), 1_000)
    assert scores == {"shared_device": 0.0, "shared_beneficiary": 0.0, "fanout": 0.0}
    assert graph.score(_tx("CUST-1", "DEV-A", "BEN-1"), 1_000) == 0.0


def test_shared_device_rises_across_two_customers() -> None:
    graph = DeviceMuleGraph(shared_device_threshold=2)
    graph.update(_tx("CUST-1", "DEV-A", "BEN-1"), 1_000)  # 1 müşteri -> 0
    scores = graph.update(_tx("CUST-2", "DEV-A", "BEN-2"), 2_000)  # 2 müşteri -> 1/3
    assert scores["shared_device"] == pytest.approx(1 / 3)
    assert scores["shared_beneficiary"] == 0.0  # alıcılar farklı
    assert scores["fanout"] == 0.0  # her müşteri tek alıcıya


def test_fanout_rises_with_five_distinct_beneficiaries() -> None:
    graph = DeviceMuleGraph()
    scores = []
    for i, benef in enumerate(("BEN-A", "BEN-B", "BEN-C", "BEN-D", "BEN-E"), start=1):
        scores.append(graph.update(_tx("CUST-1", "DEV-A", benef), i * 1_000)["fanout"])
    # 1,2 alıcı -> 0 (eşik 3 altı); 3 -> 1/3; 5 -> 1.0 (tam puan).
    assert scores[0] == 0.0
    assert scores[1] == 0.0
    assert scores[2] == pytest.approx(1 / 3)
    assert scores[3] == pytest.approx(2 / 3)
    assert scores[4] == pytest.approx(1.0)


def test_score_stays_in_unit_range_and_rings_high() -> None:
    graph = DeviceMuleGraph()
    ts = 0
    for customer in ("C1", "C2", "C3"):
        graph.update(_tx(customer, "DEV-X", "BEN-X"), ts)
        ts += 1_000
    score = graph.score(_tx("C4", "DEV-X", "BEN-X"), ts)
    assert 0.0 <= score <= 1.0
    assert score > 0.3  # paylaşılan cihaz + alıcı + fanout -> anlamlı puan


def test_window_pruning_resets_score() -> None:
    graph = DeviceMuleGraph(beneficiary_window_seconds=10)
    ts = 1_000
    for customer in ("C1", "C2"):
        graph.update(_tx(customer, "DEV-A", "BEN-1"), ts)
        ts += 1_000
    # Burst: 2 müşteri -> shared_device yükseldi.
    before = graph.update(_tx("C2", "DEV-A", "BEN-1"), ts)["shared_device"]
    assert before > 0.0
    # Pencere dışına düşünce normalleşir (tek müşteri).
    after = graph.update(_tx("C2", "DEV-A", "BEN-1"), ts + 100_000)["shared_device"]
    assert after < before


def test_customers_share_device_only_after_threshold() -> None:
    """2 müşteri eşiği aşmaz; 3 müşteri tam puan verir (gürültüye dayanıklı)."""
    graph = DeviceMuleGraph(shared_device_threshold=3)
    scores = []
    for customer in ("C1", "C2", "C3"):
        scores.append(graph.update(_tx(customer, "DEV-A", "BEN-1"), 1_000)[
            "shared_device"
        ])
    assert scores[0] == 0.0
    assert scores[1] == 0.0
    assert scores[2] == pytest.approx(1 / 3)  # 3/3 eşiğe ulaştı


def test_mule_explain_labels_only_nonzero_signals() -> None:
    assert mule_explain({}) == []
    labels = mule_explain(
        {"shared_device": 1.0, "shared_beneficiary": 0.0, "fanout": 0.4}
    )
    assert "aynı cihazdan çok sayıda farklı müşteri" in labels
    assert len(labels) == 2
