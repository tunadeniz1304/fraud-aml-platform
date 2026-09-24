"""Entity graph (P1.2): pass-through, cycles, risk propagation, rings, export."""

from __future__ import annotations

from app.graph.entity_graph import EntityGraph, GraphConfig

T0 = 1_800_000_000.0


def graph_with_customers(n: int = 6) -> EntityGraph:
    g = EntityGraph()
    for i in range(n):
        g.register_customer(f"C{i}", f"Müşteri {i}", f"TR{i:02d}")
    return g


def send(g: EntityGraph, src: str, dst_iban: str, amount: float, t: float, **kw):
    return g.observe(
        tx_id=f"{src}-{dst_iban}-{t}",
        customer_id=src,
        ts=t,
        amount=amount,
        beneficiary=dst_iban,
        **kw,
    )


class TestRealtimeSignals:
    def test_pass_through_fan_in_then_fan_out(self):
        g = graph_with_customers()
        for i, victim in enumerate(("C1", "C2", "C3")):
            quiet = send(g, victim, "TR00", 10_000, T0 + 60 * i)
            assert quiet.pass_through == 0.0 and quiet.score == 0.0
        out = send(g, "C0", "TRXX-CASH", 27_000, T0 + 600, device="DEV-R")
        assert out.fan_in_30m == 3 and out.inflow_30m == 30_000
        assert out.pass_through == 0.9
        assert any(code == "GRAPH_PASS_THROUGH" for code, _, _ in out.reasons)
        assert out.score > 0.5 and out.ring_id == "RING-0001"
        assert g.ring_id("C1") == "RING-0001" and g.ring_id("C5") is None
        # outside the 30-minute window the inflow no longer counts
        late = send(g, "C0", "TRXX-CASH", 1_000, T0 + 600 + 3600)
        assert late.fan_in_30m == 0 and late.pass_through == 0.0

    def test_cycle_detection_layering(self):
        g = graph_with_customers()
        assert not send(g, "C1", "TR02", 5_000, T0).cycle
        assert not send(g, "C2", "TR03", 4_900, T0 + 60).cycle
        closing = send(g, "C3", "TR01", 4_800, T0 + 120)
        assert closing.cycle and closing.ring_id is not None
        assert any(code == "GRAPH_CYCLE" for code, _, _ in closing.reasons)
        # rings merge: an existing member keeps the older label
        assert g.ring_id("C1") == g.ring_id("C3")

    def test_fraud_propagation_known_mule_and_shared_device(self):
        g = graph_with_customers()
        send(g, "C1", "TR00", 1_000, T0, device="D-1")
        g.flag_customer("C0")
        hit = send(g, "C2", "TR00", 500, T0 + 10)
        assert hit.known_mule_payee and hit.fraud_distance == 1  # payee's owner
        assert hit.features()["graph_known_mule_payee"] == 1.0
        g.flag_customer("C1")
        shared = send(g, "C4", "TR05", 100, T0 + 20, device="D-1")
        assert shared.shared_fraud_device and shared.fraud_distance == 2
        g.flag_account("TRX-MULE")
        assert send(g, "C5", "TRX-MULE", 10, T0 + 30).known_mule_payee
        g.flag_account("")
        clean = graph_with_customers()
        assert send(clean, "C1", "TR02", 10, T0).fraud_distance is None

    def test_hub_nodes_are_not_expanded(self):
        g = EntityGraph(GraphConfig(hub_degree=3))
        g.register_customer("F", "Fraud", "TRF")
        g.flag_customer("F")
        for i in range(6):  # everyone pays the same biller -> hub
            g.register_customer(f"P{i}", "", "")
            send(g, f"P{i}", "TR-BILLER", 100, T0 + i)
        send(g, "F", "TR-BILLER", 100, T0 + 10)
        assert send(g, "P0", "TR-BILLER", 100, T0 + 20).fraud_distance is None


class TestBatchAndExport:
    def _ring_graph(self) -> EntityGraph:
        g = graph_with_customers(10)
        for i, victim in enumerate(("C1", "C2", "C3")):
            send(g, victim, "TR00", 20_000, T0 + 60 * i)
        send(g, "C4", "TR09", 50, T0 - 3600, device="DEV-SHARED")
        send(g, "C0", "TR-CASH", 55_000, T0 + 900, device="DEV-SHARED")
        # an ordinary, unrelated community
        send(g, "C7", "TR08", 100, T0)
        send(g, "C8", "TR07", 100, T0 + 5)
        return g

    def test_detect_rings_louvain_with_summary_and_pagerank(self):
        g = self._ring_graph()
        rings = g.detect_rings()
        assert len(rings) == 1
        ring = rings[0]
        assert ring["id"] == "RING-0001"
        assert {"C0", "C1", "C2", "C3"} <= set(ring["members"])
        assert ring["stats"]["shared_devices"] >= 1
        assert ring["stats"]["total_amount_try"] >= 60_000
        assert (
            ring["stats"]["summary"].startswith("RING-0001:") and "TL" in ring["stats"]["summary"]
        )
        assert g.pagerank and max(g.pagerank.values()) == 1.0
        assert EntityGraph().detect_rings() == []

    def test_confirmed_fraud_community_is_a_ring(self):
        g = graph_with_customers(5)
        send(g, "C1", "TR02", 100, T0)  # acyclic triangle C1→C2, C1→C3, C2→C3
        send(g, "C1", "TR03", 100, T0 + 1)
        send(g, "C2", "TR03", 10, T0 + 2)  # small: not a pass-through
        assert g.detect_rings() == []  # ordinary chain, no evidence
        g.flag_customer("C2")
        rings = g.detect_rings()
        assert rings and rings[0]["id"].startswith("RING-")
        assert rings[0]["stats"]["confirmed_fraud_members"] == 1

    def test_cytoscape_neighbourhood(self):
        g = self._ring_graph()
        elements = g.neighbourhood("C0", hops=2)
        ids = {n["data"]["id"] for n in elements["nodes"]}
        assert {"C:C0", "A:TR00", "C:C1", "D:DEV-SHARED", "A:TR-CASH"} <= ids
        center = next(n for n in elements["nodes"] if n["data"]["center"])
        assert center["data"]["type"] == "customer" and center["data"]["ring"] == "RING-0001"
        transfer = next(
            e
            for e in elements["edges"]
            if e["data"]["type"] == "transfer" and e["data"]["source"] == "C:C1"
        )
        assert transfer["data"]["amount"] == 20_000 and transfer["data"]["count"] == 1
        assert all({"source", "target", "id"} <= set(e["data"]) for e in elements["edges"])
        assert g.neighbourhood("NOPE") == {"nodes": [], "edges": [], "center": "C:NOPE"}
        small = g.neighbourhood("C0", hops=3, max_nodes=3)
        assert len(small["nodes"]) <= 3

    def test_prune_bounds_memory(self):
        g = EntityGraph(GraphConfig(window_s=100, prune_every=10_000, max_nodes=5))
        for i in range(20):
            g.register_customer(f"X{i}")
            send(g, f"X{i}", f"TR-P{i}", 10, T0 + i, device=f"D{i}")
        send(g, "X0", "TR-P0", 10, T0 + 10_000)
        removed = g.prune()
        assert removed >= 19
        assert len(g.out_tx) == 1 and len(g.node_type) <= 5 + len(g.adj) + len(g.owner)
        assert g.stats()["edges"] < 60
