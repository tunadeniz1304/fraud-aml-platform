"""Deterministic data-enrichment regression tests."""

from __future__ import annotations

import json

from app import config


def _load(name: str) -> list[dict]:
    path = config.DATA_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class TestEnrichedTransactions:
    def test_all_original_ids_present(self):
        tx = _load("transactions.json")
        ids = [t["transaction_id"] for t in tx]
        for expected in range(1001, 1010):
            assert f"TX-{expected}" in ids
        customers = {t["customer_id"] for t in tx}
        for c in ("CUST-0001", "CUST-0002", "CUST-0003", "CUST-0004", "CUST-0005"):
            assert c in customers

    def test_new_fields_present(self):
        tx = _load("transactions.json")
        assert tx and all("beneficiary_id" in t for t in tx)
        assert all("ip_address" in t for t in tx)
        assert all("channel" in t for t in tx)
        assert all(t["channel"] in ("mobile", "web", "atm") for t in tx)

    def test_mule_ring_in_single_window(self):
        """TX-1002 ve TX-1004 aynı alıcıyı, 1 saatlik pencerede paylaşmalı."""
        tx = _load("transactions.json")
        by_id = {t["transaction_id"]: t for t in tx}
        a, b = by_id["TX-1002"], by_id["TX-1004"]
        assert a["beneficiary_id"] == b["beneficiary_id"]
        from datetime import datetime

        ta = datetime.fromisoformat(a["ts"])
        tb = datetime.fromisoformat(b["ts"])
        assert abs((tb - ta).total_seconds()) < 3600
        # TX-1004, akışta TX-1002'den hemen sonra işlenmeli.
        ids = [t["transaction_id"] for t in tx]
        assert ids.index("TX-1004") == ids.index("TX-1002") + 1


class TestEnrichmentReproducible:
    def test_generator_is_deterministic(self, tmp_path):
        import subprocess
        import sys

        cmd = [sys.executable, str(config.BASE_DIR / "scripts" / "enrich_data.py")]
        before = (config.DATA_DIR / "transactions.json").read_bytes()
        subprocess.run(cmd, check=True, capture_output=True, cwd=config.BASE_DIR)
        after = (config.DATA_DIR / "transactions.json").read_bytes()
        # Same instruction set -> byte-identical, idempotent.
        assert after == before
