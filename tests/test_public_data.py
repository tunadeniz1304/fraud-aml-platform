"""Public-data validation: adapters, fixtures and the offline replay (docs/DATA.md).

Everything here runs offline on ``tests/fixtures/``; tests that need the full
downloads are marked ``external_data`` and skipped when the files are absent.
"""

from __future__ import annotations

import csv
import hashlib
import json

import numpy as np
import pytest

from app.config import BASE_DIR

FIXTURES = BASE_DIR / "tests" / "fixtures"
EXTERNAL = BASE_DIR / "data" / "external"
PAYSIM_FIXTURE = FIXTURES / "paysim_sample.csv"


def test_paysim_fixture_is_small_stratified_and_attributed() -> None:
    rows = list(csv.DictReader(PAYSIM_FIXTURE.open(encoding="utf-8")))
    assert len(rows) <= 20_000
    assert PAYSIM_FIXTURE.stat().st_size < 3 * 1024 * 1024
    rate = sum(int(r["isFraud"]) for r in rows) / len(rows)
    assert 0.0010 < rate < 0.0016  # full file: 0.129 %
    assert {r["type"] for r in rows} == {"PAYMENT", "TRANSFER", "CASH_OUT", "CASH_IN", "DEBIT"}
    note = (FIXTURES / "paysim_sample.ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "cc-by-4.0" in note.lower() and "Lopez-Rojas" in note


def test_elliptic_fixture_is_verbatim_and_attributed() -> None:
    folder = FIXTURES / "elliptic_sample"
    size = sum(p.stat().st_size for p in folder.iterdir())
    assert size < 2 * 1024 * 1024
    note = (folder / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "CC BY-NC-ND 4.0" in note and "Weber" in note


def test_paysim_adapter_never_invents_fields_or_uses_leaky_columns() -> None:
    from app.datasets import paysim_adapter as ps

    txs = ps.load_transactions(PAYSIM_FIXTURE, limit=500)
    for tx in txs:
        assert tx["device_id"] == "" and tx["ip_address"] == "" and tx["session"] == {}
        assert not set(ps.EXCLUDED_COLUMNS) & set(tx)
        assert tx["channel"] in {"mobile", "web", "atm"}
    stamps = [tx["ts"] for tx in txs]
    assert stamps == sorted(stamps)
    customers = ps.customer_records(txs, prior_amount_try=1000.0)
    assert set(customers[0]) == {"customer_id", "avg_amount", "home_country"}


def test_paysim_filter_samples_whole_beneficiaries() -> None:
    from app.datasets import paysim_adapter as ps

    flt = ps.PaySimFilter(sample_frac=0.3, seed=1)
    kept = list(ps.read_rows(PAYSIM_FIXTURE, flt))
    dests = {r["nameDest"] for r in kept}
    everything = list(ps.read_rows(PAYSIM_FIXTURE))
    assert all(r in kept for r in everything if r["nameDest"] in dests)
    assert 0.2 < len(kept) / len(everything) < 0.4


async def test_engine_scores_transactions_without_device_ip_or_session() -> None:
    from app.datasets import paysim_adapter as ps
    from app.ml.training import model_feature_names
    from app.scoring.rules import load_ruleset
    from app.validation.replay import build_replay_engine, replay

    txs = ps.load_transactions(PAYSIM_FIXTURE, limit=300)
    engine = build_replay_engine(
        ps.customer_records(txs, prior_amount_try=5000.0),
        load_ruleset(),
        accounts_are_customer_ids=True,
    )
    res = await replay(txs, engine)
    assert len(res) == 300
    names = model_feature_names()
    col = names.index("device_missing")
    assert np.all(res.features[:, col] == 1.0)
    assert np.all(res.features[:, names.index("is_new_device")] == 0.0)


def test_elliptic_graph_features_are_label_free() -> None:
    """Structural features only: no feature may depend on a node's own label
    (proximity to known illicit nodes leaked the training label — see module)."""
    from app.validation import elliptic

    data = elliptic.load(FIXTURES / "elliptic_sample")
    gf = elliptic.graph_features(data)
    assert gf.shape == (len(data.ids), len(elliptic.GRAPH_FEATURES))
    flipped = elliptic.EllipticData(data.ids, data.step, data.x, 1 - data.label, data.edges)
    assert np.array_equal(gf, elliptic.graph_features(flipped))


def test_committed_validation_metrics_are_well_formed() -> None:
    root = BASE_DIR / "artifacts" / "validation"
    files = sorted(root.glob("*/metrics.json"))
    assert files, "artifacts/validation/*/metrics.json must be committed"
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["dataset"] and data["generated_at"]


@pytest.mark.external_data
def test_full_paysim_download_matches_zenodo_checksum() -> None:
    files = list((EXTERNAL / "paysim").glob("*.csv"))
    if not files:
        pytest.skip("PaySim indirilmedi (scripts/fetch_public_fraud_data.py)")
    pins = json.loads((BASE_DIR / "scripts" / "public_data_checksums.json").read_text())
    digest = hashlib.sha256()
    with files[0].open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    assert digest.hexdigest() == pins[f"paysim/{files[0].name}"]


@pytest.mark.external_data
def test_full_elliptic_has_the_published_size() -> None:
    folder = EXTERNAL / "elliptic"
    if not (folder / "elliptic_txs_classes.csv").exists():
        pytest.skip("Elliptic indirilmedi")
    from app.validation import elliptic

    data = elliptic.load(folder)
    assert len(data.ids) == 203_769 and len(data.edges) == 234_355


def test_validation_api_requires_senior_role(app_env, analyst_headers, admin_headers) -> None:
    from fastapi.testclient import TestClient

    from app.api.dashboard import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/models/validation", headers=analyst_headers).status_code == 403
        body = client.get("/api/models/validation", headers=admin_headers).json()
    assert "paysim_fixture" in body and body["paysim_fixture"]["dataset"] == "PaySim"
