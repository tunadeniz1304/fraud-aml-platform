"""Entity graph for mule-network and laundering detection (P1.2).

Nodes: customers ``C:<id>``, accounts ``A:<iban|id>``, devices ``D:<id>``, IPs
``I:<ip>``. Edges: ``owns`` (customer → own account), ``transfer`` (customer →
beneficiary account, with amount/time) and ``used`` (customer → device / IP).

**Real-time (hot path, bounded work per event):**

* *pass-through* — money received from others within 30 minutes is being sent
  on (fan-in → fan-out), the defining mule behaviour;
* *cycle* — the beneficiary can reach the sender again within 3 transfer hops
  (layering);
* *fraud proximity* — the sender/beneficiary is within 2 hops of an
  analyst-confirmed fraud node (risk propagation over the connected component);
* *known mule payee* / *device shared with a fraud customer*;
* *centrality* — cached PageRank of the beneficiary from the last batch run.

Suspicious links merge the involved customers into a **ring** (union-find), so
alerts of one network land in one case.

**Batch:** :meth:`EntityGraph.detect_rings` runs Louvain community detection on
the recent undirected projection and reports rings ("Ring #12: 7 hesap,
3 paylaşılan cihaz, toplam 1,2M TL"); PageRank is refreshed on the same pass.

Memory is bounded: edges older than the window are pruned and hub nodes (e.g.
billers with thousands of senders) are not expanded during searches.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import threading
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

MINUTE, HOUR, DAY = 60.0, 3600.0, 86400.0


@dataclass
class GraphConfig:
    window_s: float = 7 * DAY
    pass_through_window_s: float = 30 * MINUTE
    #: a pass-through needs several sources, or one large inflow
    pass_through_min_sources: int = 2
    pass_through_min_inflow: float = 10_000.0
    max_hops_cycle: int = 3
    #: layering cycles are fast and move similar amounts
    cycle_window_s: float = DAY
    cycle_amount_band: tuple[float, float] = (0.5, 2.0)
    hub_degree: int = 200
    max_nodes: int = 200_000
    prune_every: int = 2_000
    min_ring_customers: int = 3


@dataclass
class GraphSignals:
    score: float = 0.0
    pass_through: float = 0.0
    fan_in_30m: int = 0
    inflow_30m: float = 0.0
    cycle: bool = False
    fraud_distance: int | None = None
    known_mule_payee: bool = False
    shared_fraud_device: bool = False
    payee_centrality: float = 0.0
    ring_id: str | None = None
    reasons: list[tuple[str, str, float]] = field(default_factory=list)

    def features(self) -> dict[str, float]:
        return {
            "graph_score": round(self.score, 4),
            "graph_pass_through": round(self.pass_through, 4),
            "graph_fan_in_30m": float(self.fan_in_30m),
            "graph_cycle": 1.0 if self.cycle else 0.0,
            "graph_fraud_distance": float(self.fraud_distance)
            if self.fraud_distance is not None
            else -1.0,
            "graph_known_mule_payee": 1.0 if self.known_mule_payee else 0.0,
        }


def _noisy_or(values: Iterable[float]) -> float:
    rest = 1.0
    for v in values:
        rest *= 1.0 - max(0.0, min(1.0, v))
    return 1.0 - rest


class EntityGraph:
    def __init__(self, config: GraphConfig | None = None) -> None:
        self.cfg = config or GraphConfig()
        self._lock = threading.RLock()
        self.node_type: dict[str, str] = {}
        self.node_label: dict[str, str] = {}
        self.owner: dict[str, str] = {}  # account node -> customer node
        self.accounts_of: dict[str, set[str]] = defaultdict(set)  # customer -> accounts
        # adjacency with last-seen time (undirected view for proximity/export)
        self.adj: dict[str, dict[str, float]] = defaultdict(dict)
        # transfers: customer -> deque[(ts, account, amount, tx_id)]
        self.out_tx: dict[str, deque[tuple[float, str, float, str]]] = defaultdict(deque)
        # account -> deque[(ts, sender customer, amount)]
        self.in_tx: dict[str, deque[tuple[float, str, float]]] = defaultdict(deque)
        self.edge_stats: dict[
            tuple[str, str, str], list[float]
        ] = {}  # (u,v,type) -> [n, sum, last]
        self.fraud_nodes: set[str] = set()
        self.pagerank: dict[str, float] = {}
        self._ring_parent: dict[str, str] = {}
        self._ring_label: dict[str, str] = {}
        self._ring_seq = 0
        self._updates = 0
        self.now = 0.0

    # --- node helpers ------------------------------------------------------------------
    @staticmethod
    def customer_node(customer_id: str) -> str:
        return f"C:{customer_id}"

    @staticmethod
    def account_node(account: str) -> str:
        return f"A:{account}"

    def _node(self, node: str, kind: str, label: str) -> None:
        if node not in self.node_type:
            self.node_type[node] = kind
            self.node_label[node] = label

    def _link(self, a: str, b: str, kind: str, ts: float, amount: float = 0.0) -> None:
        self.adj[a][b] = ts
        self.adj[b][a] = ts
        stats = self.edge_stats.get((a, b, kind))
        if stats is None:
            self.edge_stats[(a, b, kind)] = [1.0, amount, ts]
        else:
            stats[0] += 1
            stats[1] += amount
            stats[2] = ts

    def register_customer(self, customer_id: str, name: str = "", iban: str = "") -> None:
        with self._lock:
            c = self.customer_node(customer_id)
            self._node(c, "customer", name or customer_id)
            if iban:
                a = self.account_node(iban)
                self._node(a, "account", iban)
                self.owner[a] = c
                self.accounts_of[c].add(a)
                self._link(c, a, "owns", 0.0)

    def flag_fraud(self, *nodes: str) -> None:
        with self._lock:
            self.fraud_nodes.update(n for n in nodes if n)

    def flag_customer(self, customer_id: str) -> None:
        self.flag_fraud(self.customer_node(customer_id))

    def flag_account(self, account: str) -> None:
        if account:
            self.flag_fraud(self.account_node(account))

    # --- ring union-find ----------------------------------------------------------------
    def _find(self, node: str) -> str:
        parent = self._ring_parent.setdefault(node, node)
        if parent != node:
            root = self._find(parent)
            self._ring_parent[node] = root
            return root
        return node

    def _union(self, a: str, b: str) -> str:
        ra, rb = self._find(a), self._find(b)
        if ra == rb:
            return ra
        # keep the older ring label
        la, lb = self._ring_label.get(ra), self._ring_label.get(rb)
        if la and (not lb or la <= lb):
            self._ring_parent[rb] = ra
            return ra
        self._ring_parent[ra] = rb
        return rb

    def _ring_of(self, *members: str) -> str:
        root = members[0]
        for m in members[1:]:
            root = self._union(root, m)
        root = self._find(root)
        if root not in self._ring_label:
            self._ring_seq += 1
            self._ring_label[root] = f"RING-{self._ring_seq:04d}"
        return self._ring_label[root]

    def ring_id(self, customer_id: str) -> str | None:
        node = self.customer_node(customer_id)
        if node not in self._ring_parent:
            return None
        return self._ring_label.get(self._find(node))

    # --- real-time update -------------------------------------------------------------------
    def observe(
        self,
        *,
        tx_id: str,
        customer_id: str,
        ts: float,
        amount: float,
        beneficiary: str = "",
        device: str = "",
        ip: str = "",
    ) -> GraphSignals:
        """Add the transfer to the graph and return its graph signals."""
        with self._lock:
            self.now = max(self.now, ts)
            c = self.customer_node(customer_id)
            self._node(c, "customer", customer_id)
            signals = GraphSignals()
            payee = self.account_node(beneficiary) if beneficiary else None
            if device:
                d = f"D:{device}"
                self._node(d, "device", device)
                self._link(c, d, "used", ts)
                signals.shared_fraud_device = any(
                    n in self.fraud_nodes and n != c
                    for n in self.adj[d]
                    if self.node_type.get(n) == "customer"
                )
            if ip:
                i = f"I:{ip}"
                self._node(i, "ip", ip)
                self._link(c, i, "used", ts)

            # pass-through: inflow to this customer's accounts in the last 30 min
            since = ts - self.cfg.pass_through_window_s
            inflow, senders = 0.0, set()
            for account in self.accounts_of.get(c, ()):
                for t, sender, amt in self.in_tx.get(account, ()):
                    if since <= t <= ts and sender != c:
                        inflow += amt
                        senders.add(sender)
            outflow = sum(a for t, _, a, _ in self.out_tx.get(c, ()) if since <= t <= ts)
            signals.inflow_30m = inflow
            signals.fan_in_30m = len(senders)
            if inflow > 0 and amount > 0:
                signals.pass_through = min(1.0, (outflow + amount) / inflow)

            if payee is not None:
                self._node(payee, "account", beneficiary)
                owner = self.owner.get(payee)
                signals.known_mule_payee = payee in self.fraud_nodes or (
                    owner is not None and owner in self.fraud_nodes
                )
                signals.cycle = (
                    self._reaches(owner, c, ts=ts, amount=amount) if owner and owner != c else False
                )
                signals.payee_centrality = self.pagerank.get(payee, 0.0)
                self._link(c, payee, "transfer", ts, amount)
                self.out_tx[c].append((ts, payee, amount, tx_id))
                self.in_tx[payee].append((ts, c, amount))
            signals.fraud_distance = self._fraud_distance(c, payee)

            parts: list[tuple[str, str, float]] = []
            if signals.known_mule_payee:
                parts.append(
                    ("GRAPH_KNOWN_MULE_PAYEE", "Alıcı hesap doğrulanmış fraud ağında", 0.9)
                )
            significant_inflow = (
                signals.fan_in_30m >= self.cfg.pass_through_min_sources
                or signals.inflow_30m >= self.cfg.pass_through_min_inflow
            )
            if not significant_inflow:
                signals.pass_through = 0.0
            if signals.pass_through >= 0.7 and signals.fan_in_30m >= 1:
                parts.append(
                    (
                        "GRAPH_PASS_THROUGH",
                        f"Son 30 dakikada {signals.fan_in_30m} farklı kaynaktan gelen para "
                        "hızla aktarılıyor (fan-in → fan-out, aktarım oranı "
                        f"%{min(100, round(signals.pass_through * 100))})",
                        0.6 * signals.pass_through,
                    )
                )
            if signals.cycle:
                parts.append(("GRAPH_CYCLE", "Para döngüsü (katmanlama) tespit edildi", 0.6))
            if signals.shared_fraud_device:
                parts.append(
                    ("GRAPH_SHARED_FRAUD_DEVICE", "Cihaz doğrulanmış fraud müşterisiyle ortak", 0.7)
                )
            if signals.fraud_distance == 1:
                parts.append(("GRAPH_FRAUD_NEIGHBOUR", "Fraud düğümüyle doğrudan bağlantı", 0.5))
            elif signals.fraud_distance == 2:
                parts.append(("GRAPH_FRAUD_PROXIMITY", "Fraud düğümüne 2 adım mesafe", 0.25))
            if signals.payee_centrality >= 0.8:
                parts.append(("GRAPH_CENTRAL_PAYEE", "Alıcı, ağda toplama merkezi konumunda", 0.3))
            signals.reasons = parts
            signals.score = round(_noisy_or(w for _, _, w in parts), 4)

            suspicious_link = signals.pass_through >= 0.7 or signals.cycle
            if suspicious_link:
                # money direction: a fan-in source that never received money itself
                # is an affected victim, not a ring member
                members = [c, *(x for x in senders if self._has_inflow(x, since))]
                if payee is not None:
                    members.append(self.owner.get(payee, payee))
                signals.ring_id = self._ring_of(*members)
            elif c in self._ring_parent:
                signals.ring_id = self._ring_label.get(self._find(c))

            self._updates += 1
            if self._updates % self.cfg.prune_every == 0:
                self.prune()
            return signals

    def _has_inflow(self, customer: str, since: float) -> bool:
        return any(
            t >= since and sender != customer
            for account in self.accounts_of.get(customer, ())
            for t, sender, _ in self.in_tx.get(account, ())
        )

    def flag_device(self, device: str) -> None:
        if device:
            self.flag_fraud(f"D:{device}")

    def _neighbours(self, node: str) -> Iterable[str]:
        nbrs = self.adj.get(node, {})
        return () if len(nbrs) > self.cfg.hub_degree else nbrs.keys()

    def _reaches(self, start: str, target: str, *, ts: float, amount: float) -> bool:
        """Did similar-sized money flow from ``start`` back to ``target`` within
        N transfer hops and the cycle window (layering)?"""
        frontier, seen = [start], {start}
        cutoff = ts - self.cfg.cycle_window_s
        low, high = (amount * f for f in self.cfg.cycle_amount_band)
        for _ in range(self.cfg.max_hops_cycle):
            nxt: list[str] = []
            for cust in frontier:
                for t, account, value, _ in self.out_tx.get(cust, ()):
                    if t < cutoff or t > ts or not low <= value <= high:
                        continue
                    owner = self.owner.get(account)
                    if owner == target:
                        return True
                    if owner and owner not in seen:
                        seen.add(owner)
                        nxt.append(owner)
            frontier = nxt
            if not frontier:
                break
        return False

    def _fraud_distance(self, c: str, payee: str | None) -> int | None:
        if not self.fraud_nodes:
            return None
        starts = [n for n in (c, payee) if n]
        if any(n in self.fraud_nodes for n in starts if n != c):
            return 0
        frontier, seen = list(starts), set(starts)
        for depth in (1, 2):
            nxt: list[str] = []
            for node in frontier:
                for n in self._neighbours(node):
                    if n in seen:
                        continue
                    if n in self.fraud_nodes and n != c:
                        return depth
                    seen.add(n)
                    nxt.append(n)
            frontier = nxt
        return None

    # --- maintenance -------------------------------------------------------------------------
    def prune(self) -> int:
        """Drop edges older than the window; returns removed transfer records."""
        with self._lock:
            cutoff = self.now - self.cfg.window_s
            removed = 0
            for store in (self.out_tx, self.in_tx):
                for key in list(store):
                    q = store[key]
                    while q and q[0][0] < cutoff:
                        q.popleft()
                        removed += 1
                    if not q:
                        del store[key]
            for node in list(self.adj):
                nbrs = self.adj[node]
                for other in [o for o, t in nbrs.items() if 0 < t < cutoff]:
                    del nbrs[other]
                if not nbrs and node not in self.fraud_nodes and node not in self.owner:
                    del self.adj[node]
            for edge in [k for k, v in self.edge_stats.items() if 0 < v[2] < cutoff]:
                del self.edge_stats[edge]
            if len(self.node_type) > self.cfg.max_nodes:
                keep = set(self.adj) | self.fraud_nodes | set(self.owner)
                for node in [n for n in self.node_type if n not in keep]:
                    self.node_type.pop(node, None)
                    self.node_label.pop(node, None)
            return removed

    # --- export / batch ------------------------------------------------------------------------
    def neighbourhood(
        self, customer_id: str, hops: int = 2, max_nodes: int = 150
    ) -> dict[str, Any]:
        """Cytoscape.js elements around a customer."""
        with self._lock:
            start = self.customer_node(customer_id)
            if start not in self.node_type:
                return {"nodes": [], "edges": [], "center": start}
            seen, frontier = {start}, [start]
            for _ in range(max(1, min(hops, 3))):
                nxt = []
                for node in frontier:
                    for n in self._neighbours(node):
                        if n not in seen and len(seen) < max_nodes:
                            seen.add(n)
                            nxt.append(n)
                frontier = nxt
            nodes = [
                {
                    "data": {
                        "id": n,
                        "label": self.node_label.get(n, n),
                        "type": self.node_type.get(n, "?"),
                        "fraud": n in self.fraud_nodes,
                        "ring": self._ring_label.get(self._find(n))
                        if n in self._ring_parent
                        else None,
                        "center": n == start,
                    }
                }
                for n in sorted(seen)
            ]
            edges = [
                {
                    "data": {
                        "id": f"{u}->{v}:{kind}",
                        "source": u,
                        "target": v,
                        "type": kind,
                        "count": int(stats[0]),
                        "amount": round(stats[1], 2),
                    }
                }
                for (u, v, kind), stats in self.edge_stats.items()
                if u in seen and v in seen
            ]
            return {"nodes": nodes, "edges": edges, "center": start}

    def to_networkx(self) -> nx.Graph:
        """Undirected projection (customers linked via transfers and shared devices)."""
        with self._lock:
            g = nx.Graph()
            for (u, v, kind), stats in self.edge_stats.items():
                if kind == "owns":
                    continue
                w = math.log1p(stats[1]) + stats[0] if kind == "transfer" else 2.0
                # collapse accounts onto their owner so money paths connect customers
                a = self.owner.get(u, u)
                b = self.owner.get(v, v)
                if a == b:
                    continue
                if g.has_edge(a, b):
                    g[a][b]["weight"] += w
                else:
                    g.add_edge(a, b, weight=w, kind=kind)
            return g

    def detect_rings(self, seed: int = 42) -> list[dict[str, Any]]:
        """Louvain communities that look like mule rings (+ PageRank refresh)."""
        with self._lock:
            projection = self.to_networkx()
            directed = nx.DiGraph()
            for (u, v, kind), stats in self.edge_stats.items():
                if kind == "transfer":
                    directed.add_edge(u, v, weight=stats[1])
        if directed.number_of_edges():
            pr = nx.pagerank(directed, weight="weight")
            accounts = {n: v for n, v in pr.items() if n.startswith("A:")}
            ranked = sorted(accounts.values())
            # percentile rank: robust to graph size, comparable across runs
            self.pagerank = {
                n: bisect.bisect_right(ranked, v) / len(ranked) for n, v in accounts.items()
            }
        if projection.number_of_nodes() == 0:
            return []
        communities = nx.community.louvain_communities(projection, weight="weight", seed=seed)
        rings: list[dict[str, Any]] = []
        with self._lock:
            for community in communities:
                customers = sorted(n for n in community if self.node_type.get(n) == "customer")
                if len(customers) < self.cfg.min_ring_customers:
                    continue
                # devices of ring members that are also used by another customer
                devices = {
                    n
                    for c in customers
                    for n in self.adj.get(c, {})
                    if self.node_type.get(n) == "device"
                    and sum(1 for x in self.adj.get(n, {}) if self.node_type.get(x) == "customer")
                    >= 2
                }
                internal = [
                    stats
                    for (u, v, kind), stats in self.edge_stats.items()
                    if kind == "transfer" and u in community and self.owner.get(v, v) in community
                ]
                total = sum(s[1] for s in internal)
                flagged = [c for c in customers if c in self.fraud_nodes]
                linked = [c for c in customers if c in self._ring_parent]
                affected = self._affected(customers, community, devices, flagged)
                if not (devices or flagged or len(linked) >= 2):
                    continue  # ordinary community (family, colleagues)
                labels = {self._ring_label.get(self._find(c)) for c in linked}
                labels.discard(None)
                ring_id = (
                    min(labels)  # type: ignore[type-var]
                    if labels
                    else "RING-"
                    + hashlib.sha1("|".join(customers).encode(), usedforsecurity=False)
                    .hexdigest()[:8]
                    .upper()
                )
                members = [c[2:] for c in customers if c not in affected]
                rings.append(
                    {
                        "id": ring_id,
                        "members": members,
                        "affected": [c[2:] for c in affected],
                        "stats": {
                            "accounts": len(members),
                            "affected": [c[2:] for c in affected],
                            "shared_devices": len(devices),
                            "internal_transfers": int(sum(s[0] for s in internal)),
                            "total_amount_try": round(total, 2),
                            "confirmed_fraud_members": len(flagged),
                            "summary": (
                                f"{ring_id}: {len(members)} hesap, {len(devices)} "
                                f"paylaşılan cihaz, toplam {_tl(total)}"
                            ),
                        },
                    }
                )
        rings.sort(key=lambda r: -r["stats"]["total_amount_try"])
        return rings

    def _affected(
        self, customers: list[str], community: set[str], devices: set[str], flagged: list[str]
    ) -> set[str]:
        """Customers who only *send* into the community: fan-in sources (victims).

        A member receives money from the community, shares one of its devices or
        is confirmed fraud; everybody else merely paid into it.
        """
        out = set()
        for c in customers:
            if c in flagged or any(n in devices for n in self.adj.get(c, {})):
                continue
            received = any(
                sender in community
                for account in self.accounts_of.get(c, ())
                for _, sender, _ in self.in_tx.get(account, ())
            )
            if not received:
                out.add(c)
        return out

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "nodes": len(self.node_type),
                "edges": len(self.edge_stats),
                "fraud_nodes": len(self.fraud_nodes),
                "rings": len({self._find(n) for n in self._ring_parent}),
            }


def _tl(amount: float) -> str:
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M TL".replace(".", ",")
    if amount >= 1_000:
        return f"{amount / 1_000:.0f}B TL"
    return f"{amount:.0f} TL"
