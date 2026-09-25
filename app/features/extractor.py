"""Feature extraction orchestration (same code online and in training)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.signals import IPIntel, default_ip_intel
from app.features.definitions import compute_features
from app.features.profile import learn
from app.features.store import FeatureStateStore, MemoryFeatureStore
from app.features.types import CustomerStatic, FeatureContext, ProfileState, TxView


class CustomerDirectory:
    """Static (KYC) customer data keyed by id."""

    def __init__(self, records: Iterable[dict[str, Any]] = ()) -> None:
        self._by_id: dict[str, CustomerStatic] = {}
        self._raw: dict[str, dict[str, Any]] = {}
        for record in records:
            self.add(record)

    @classmethod
    def from_file(cls, path: Path) -> CustomerDirectory:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(data if isinstance(data, list) else [data])

    def add(self, record: dict[str, Any]) -> None:
        static = CustomerStatic.from_record(record)
        self._by_id[static.customer_id] = static
        self._raw[static.customer_id] = record

    def get(self, customer_id: str) -> CustomerStatic | None:
        return self._by_id.get(customer_id)

    def raw(self, customer_id: str) -> dict[str, Any] | None:
        return self._raw.get(customer_id)

    def __contains__(self, customer_id: object) -> bool:
        return customer_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def names(self) -> list[str]:
        return [c.name for c in self._by_id.values() if c.name]

    def records(self) -> list[dict[str, Any]]:
        return list(self._raw.values())


@dataclass
class Extraction:
    tx: TxView
    context: FeatureContext
    features: dict[str, float]


class FeatureExtractor:
    """Read state → compute features; later commit the event (+ learning)."""

    def __init__(
        self,
        store: FeatureStateStore | None = None,
        customers: CustomerDirectory | None = None,
        *,
        ip_intel: IPIntel | None = None,
    ) -> None:
        self.store: FeatureStateStore = store or MemoryFeatureStore(
            get_settings().feature_max_entities
        )
        self.customers = customers or CustomerDirectory()
        self.ip_intel = ip_intel or default_ip_intel()

    def _initial_profile(self, customer: CustomerStatic) -> ProfileState:
        return ProfileState.from_static(customer, get_settings().profile_prior_weight)

    async def extract(
        self, payload: dict[str, Any] | TxView, *, extras: dict[str, float] | None = None
    ) -> Extraction:
        tx = payload if isinstance(payload, TxView) else TxView.from_payload(payload)
        customer = self.customers.get(tx.customer_id) or CustomerStatic(tx.customer_id)
        state = await self.store.snapshot(tx)
        profile = state.profile or self._initial_profile(customer)
        ctx = FeatureContext(
            tx=tx,
            customer=customer,
            state=state,
            profile=profile,
            ip=self.ip_intel.lookup(tx.ip_address),
            extras=dict(extras or {}),
        )
        return Extraction(tx, ctx, compute_features(ctx))

    async def commit(self, extraction: Extraction, *, learn_profile: bool) -> ProfileState:
        """Record the event in all windows; update the profile only if trusted."""
        profile = extraction.context.profile
        if learn_profile:
            profile = learn(profile, extraction.tx, alpha_min=get_settings().profile_alpha)
        await self.store.commit(extraction.tx, profile)
        return profile

    async def learn_event(self, tx: TxView) -> ProfileState:
        """Late learning from an already committed event (step-up / analyst feedback)."""
        state = await self.store.snapshot(tx)
        customer = self.customers.get(tx.customer_id) or CustomerStatic(tx.customer_id)
        profile = state.profile or self._initial_profile(customer)
        profile = learn(profile, tx, alpha_min=get_settings().profile_alpha)
        await self.store.put_profile(tx.customer_id, profile)
        return profile
