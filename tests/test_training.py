"""Synthetic data generator + end-to-end training pipeline (P0.7)."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from app.api.schemas import TransactionIn
from app.core.sanctions import SanctionScreener
from app.ml.registry import ModelRegistry
from app.ml.training import TrainingConfig, train
from app.synthetic.generator import (
    SyntheticConfig,
    generate,
    iban_check,
    load_dataset,
    make_iban,
    strip_labels,
)

SMALL = {
    "customers": 150,
    "days": 30,
    "ato_attacks": 16,
    "app_scams": 18,
    "mule_rings": 2,
    "ring_size": 4,
    "card_testing": 8,
    "structuring": 8,
    "sanctions_hits": 3,
}


@pytest.fixture(scope="module")
def small():
    return generate(SyntheticConfig(seed=11, **SMALL))


class TestGenerator:
    def test_same_seed_same_data(self, small):
        again = generate(SyntheticConfig(seed=11, **SMALL))
        assert again.transactions == small.transactions
        assert again.customers == small.customers
        other = generate(SyntheticConfig(seed=12, **SMALL))
        assert other.transactions != small.transactions

    def test_every_typology_is_labelled(self, small):
        typologies = Counter(t["typology"] for t in small.transactions)
        assert set(typologies) == {
            "normal",
            "ato",
            "app",
            "mule",
            "card_testing",
            "structuring",
            "sanctions",
        }
        for tx in small.transactions:
            assert tx["label"] == (0 if tx["typology"] == "normal" else 1)
        sanctions = [t for t in small.transactions if t["typology"] == "sanctions"]
        assert all(t["exclude_from_training"] for t in sanctions)
        screener = SanctionScreener().load()
        assert all(screener.screen(t["beneficiary_name"]) for t in sanctions)

    def test_transactions_are_valid_api_payloads_and_time_ordered(self, small):
        ts = [t["ts"] for t in small.transactions]
        assert ts == sorted(ts)
        for tx in small.transactions[:500] + small.transactions[-200:]:
            TransactionIn.model_validate(strip_labels(tx))
        ids = [t["transaction_id"] for t in small.transactions]
        assert len(set(ids)) == len(ids)

    def test_realistic_structure(self, small):
        summary = small.summary()
        assert 0.005 < summary["fraud_rate"] < 0.05
        app = [t for t in small.transactions if t["typology"] == "app"]
        # APP victims use their own (known) device; the typed name differs from the registry
        registry = {p["iban"]: p for p in small.payees}
        assert any(t["beneficiary_name"] != registry[t["beneficiary_iban"]]["name"] for t in app)
        assert sum(r == "mule" for r in small.roles.values()) == 8
        assert all(
            registry[c["iban"]]["is_mule"]
            for c in small.customers
            if small.roles[c["customer_id"]] == "mule"
        )
        structuring = [t for t in small.transactions if t["typology"] == "structuring"]
        assert all(45_000 <= t["amount"] < 50_000 for t in structuring)

    def test_iban_checksum(self):
        import random

        iban = make_iban(random.Random(1))
        assert len(iban) == 26 and iban.startswith("TR")
        assert iban_check(iban[4:]) == iban[2:4]
        digits = "".join(str(int(ch, 36)) for ch in iban[4:] + iban[:4])
        assert int(digits) % 97 == 1

    def test_write_and_load(self, small, tmp_path):
        paths = small.write(tmp_path)
        customers, txs = load_dataset(tmp_path)
        assert len(customers) == 150 and len(txs) == len(small.transactions)
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        assert manifest["seed"] == 11 and manifest["rings"]
        assert not any(k.startswith("_") for c in customers for k in c)


class TestTraining:
    def test_training_is_deterministic_and_useful(self, small, tmp_path):
        reports = []
        for run in ("a", "b"):
            cfg = TrainingConfig(
                seed=5,
                version="m_test",
                models_dir=tmp_path / run,
                num_boost_round=120,
                iforest_estimators=40,
                backtest_path=tmp_path / run / "backtest.jsonl",
                data_manifest=small.summary(),
            )
            reports.append(train(small.customers, small.transactions, cfg))
        a, b = reports
        assert a.metrics["test"] == b.metrics["test"]
        hybrid = a.metrics["test"]["hybrid"]
        assert hybrid["pr_auc"] > 0.6
        assert a.metrics["test"]["ml"]["roc_auc"] > 0.8
        assert a.seconds < 120
        card = (a.path / "model_card.md").read_text(encoding="utf-8")
        assert "PR-AUC" in card and "Maliyet ağırlıklı recall" in card
        meta = json.loads((a.path / "metadata.json").read_text(encoding="utf-8"))
        assert "score" in meta["psi_reference"] and len(meta["features"]) >= 40
        registry = ModelRegistry(tmp_path / "a")
        assert registry.champion_version() == "m_test"
        assert registry.models()[0]["metrics"]["pr_auc"] == hybrid["pr_auc"]
        lines = (tmp_path / "a" / "backtest.jsonl").read_text(encoding="utf-8").splitlines()
        assert lines and {"id", "y", "f"} <= set(json.loads(lines[0]))
        assert "m_test" in a.headline()
