"""DeviceMuleGraph için odaklı testler.

Deterministik, pencere/yürütme sırası sabit sözlüklerle bitecek şekilde
standart işlem kalıplarını kullanır.
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


def test_shared_device_rises_across_two_customers_same_device() -> None:
    graph = DeviceMuleGraph()
    first = graph.update(
        _tx("CUST-1", "DEV-A", "BEN-1"), 1_000
    )["shared_device"]
    second = graph.update(
        _tx("CUST-2", "DEV-A", "BEN-1"), 2_000
    )["shared_device"]
    assert first == pytest.approx(0.5)  # 1 müşteri / eşik 2
    assert second == pytest.approx(1.0)  # 2 müşteri / eşik 2


def test_fanout_rises_with_three_distinct_beneficiaries() -> None:
    graph = DeviceMuleGraph()
    scores = []
    for i, beneficiary in enumerate(("BEN-A", "BEN-B", "BEN-C"), start=1):
        scores.append(
            graph.update(
                _tx("CUST-1", "DEV-A", beneficiary), i * 1_000
            )["fanout"]
        )
    # 1, 2 ve 3 farklı alıcı -> sırasıyla 1/3, 2/3, 3/3
    assert scores == pytest.approx([1 / 3, 2 / 3, 1.0])


def test_score_stays_in_unit_range() -> None:
    graph = DeviceMuleGraph()
    # Çok sayıda farklı müşteri + alıcı + yüksek fanout -> puan 1'e değmeli.
    ts = 0
    for customer in ("C1", "C2"):
        for beneficiary in ("BA", "BB", "BC"):
            graph.update(_tx(customer, "DEV-X", beneficiary), ts)
            ts += 1_000
    score = graph.score(_tx("C3", "DEV-X", "BD"), ts)
    assert 0.0 <= score <= 1.0


def test_window_pruning_resets_score() -> None:
    graph = DeviceMuleGraph(beneficiary_window_seconds=10)
    # Aynı cihazı 3 farklı müşteri paylaşır -> shared_device = 1.0
    ts = 1_000
    for customer in ("C1", "C2", "C3"):
        graph.update(_tx(customer, "DEV-A", "BEN-1"), ts)
        ts += 1_000
    before = graph.update(_tx("C4", "DEV-A", "BEN-1"), ts)["shared_device"]
    assert before == pytest.approx(1.0)

    # Pencereden (10 sn) çok sonra yeni bir işlem: eski kayıtlar budanır,
    # cihazda yalnızca bu tek müşteri kalır -> puan sıfıra döner.
    after = graph.update(
        _tx("C4", "DEV-A", "BEN-1"), ts + 100_000
    )["shared_device"]
    assert after == pytest.approx(0.5)  # tek müşteri / eşik 2


def test_mule_explain_labels_only_nonzero_signals() -> None:
    assert mule_explain({}) == []
    assert mule_explain(
        {"shared_device": 1.0, "shared_beneficiary": 0.0, "fanout": 0.4}
    ) == ["aynı cihazdan çok sayıda farklı müşteri", "tek müşteriden çok sayıda farklı alıcıya"]
