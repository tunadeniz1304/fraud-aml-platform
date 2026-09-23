"""Synthetic data enrichment (deterministic, ids preserved).

Loads data/customers.json + data/transactions.json and adds fraud-realistic
fields: transactions gain beneficiary_id/ip_address/channel; the two already-
blocked TX-1002/TX-1004 share a beneficiary so the money-mule graph sees a real
ring. Counts and ids unchanged -> existing tests unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "data"

TX_ENRICH = {
    "TX-1001": {"bene_id": "BEN-SAL-1", "ip": "88.241.10.5", "ch": "web"},
    "TX-1002": {"bene_id": "BEN-SHM-1", "ip": "197.210.44.9", "ch": "web"},
    "TX-1003": {"bene_id": "BEN-INV-1", "ip": "5.176.120.33", "ch": "mobile"},
    "TX-1004": {"bene_id": "BEN-SHM-1", "ip": "89.12.77.140", "ch": "web"},
    "TX-1005": {"bene_id": "BEN-RTL-1", "ip": "176.40.99.210", "ch": "mobile"},
    "TX-1006": {"bene_id": "BEN-FAM-1", "ip": "176.42.11.88", "ch": "mobile"},
    "TX-1007": {"bene_id": "BEN-BUS-1", "ip": "178.233.50.4", "ch": "web"},
    "TX-1008": {"bene_id": "BEN-GFT-1", "ip": "92.96.188.70", "ch": "mobile"},
    "TX-1009": {"bene_id": "BEN-P2P-1", "ip": "88.241.12.77", "ch": "mobile"},
}


def enrich() -> None:
    """Add fields in place, preserving record order and ids."""
    for name, key_old, fields in (
        ("transactions.json", "transaction_id", TX_ENRICH),
    ):
        path = BASE / name
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for record in data:
            extra = fields.get(record[key_old])
            if extra:
                record["beneficiary_id"] = extra["bene_id"]
                record["ip_address"] = extra["ip"]
                record["channel"] = extra["ch"]
        # TX-1002 (02:15) ve TX-1004 ortak BEN-SHM-1 alıcısını 1 saatlik
        # pencerede paylaşsın (mule halkası görünür olsun diye). TX-1004'ün
        # kaydı TX-1002'den hemen sonraya taşınır; aksi halde aradaki TX-1003
        # (10:05) işlenirken pencere budaması TX-1002'yi düşürür, halka asla
        # yakalanamaz (akış dosya sırasıyla işlenir).
        for record in data:
            if record.get("transaction_id") == "TX-1004":
                record["ts"] = "2026-09-22T02:16:00"
        idx_1002 = next(i for i, r in enumerate(data) if r["transaction_id"] == "TX-1002")
        idx_1004 = next(i for i, r in enumerate(data) if r["transaction_id"] == "TX-1004")
        if idx_1004 != idx_1002 + 1:
            rec = data.pop(idx_1004)
            data.insert(idx_1002 + 1, rec)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    print("Veri zenginleştirildi (id'ler korundu).")


if __name__ == "__main__":
    enrich()
