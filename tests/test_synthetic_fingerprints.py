"""The synthetic generator must not leak its labels through fingerprints (§1.3).

Before v2 the generator stamped account takeovers with ``DEV-ATO-*`` devices
that were new 85 % of the time (legitimate traffic: 1.5 %), so
``is_new_device`` + ``device_age_d`` carried 54 % of the model's gain and the
0.97 PR-AUC validated the generator rather than the method.
"""

from __future__ import annotations

import re
from collections import Counter

import numpy as np
import pytest

from app.ml.training import TrainingConfig, train
from app.synthetic.generator import SyntheticConfig, generate

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
MAX_FEATURE_SHARE = 0.35
#: flipped legitimate payments may be at most this share of the positive labels
MAX_FLIPPED_NORMAL_SHARE = 0.10
#: depth-4 tree on purpose + IP + beneficiary name only. Before v3 these fields
#: alone reached AP 0.88; what remains is typology semantics (fraud never pays
#: utility bills or withdraws cash at an ATM), not generator artefacts.
MAX_TEXT_NETWORK_AP = 0.30
MAX_TEXT_NETWORK_LIFT = 8.0
#: detector AUC that still counts as "chance" for a few hundred positives
CHANCE_AUC = 0.60


@pytest.fixture(scope="module")
def data():
    return generate(SyntheticConfig(seed=11, **SMALL))


def _id_features(tx: dict) -> list[float]:
    """Only what an identifier string itself reveals (format, prefix, charset).

    The identity of an entity (a specific mule IBAN seen again) is a legitimate
    signal, not a fingerprint, so no per-value hashing here.
    """
    out: list[float] = []
    for key in ("device_id", "beneficiary_iban", "transaction_id"):
        value = str(tx.get(key) or "")
        parts = value.split("-")
        out += [
            float(len(value)),
            float(len(parts)),
            float(len(parts[0])),
            float(sum(ch.isdigit() for ch in value)),
            float(sum(ch.isalpha() for ch in value)),
            float(bool(re.search(r"[G-Z]", value.split("-", 1)[-1]))),  # non-hex letters
        ]
    return out


def test_device_ids_share_one_typology_independent_format(data) -> None:
    formats = Counter(
        (t["typology"], "DEV-" + re.sub(r"[0-9A-F]", "x", t["device_id"].removeprefix("DEV-")))
        for t in data.transactions
        if t["device_id"]
    )
    shapes = {shape for _, shape in formats}
    assert shapes == {"DEV-xxxxxxxx"}


def test_overlapping_device_distributions(data) -> None:
    known = {c["customer_id"]: set(c["known_device_ids"]) for c in data.customers}

    def new_rate(rows: list[dict]) -> float:
        return sum(t["device_id"] not in known[t["customer_id"]] for t in rows) / len(rows)

    normal = [t for t in data.transactions if t["typology"] == "normal" and t["device_id"]]
    ato = [t for t in data.transactions if t["typology"] == "ato"]
    assert new_rate(normal) > 0.04  # phone changes, borrowed phones, new browsers
    assert new_rate(ato) < 0.85  # part of the takeovers come from the known device


def test_label_noise_is_applied(data) -> None:
    noisy = [t for t in data.transactions if t.get("label_noise")]
    assert noisy
    assert all(t["label"] == (0 if t["typology"] == "normal" else 1) ^ 1 for t in noisy)


def test_label_noise_is_asymmetric(data) -> None:
    """Noise is mostly missed fraud; flipped normals are a small share of positives."""
    noisy = [t for t in data.transactions if t.get("label_noise")]
    missed = sum(t["typology"] != "normal" for t in noisy)
    disputed = sum(t["typology"] == "normal" for t in noisy)
    positives = sum(t["label"] for t in data.transactions if not t["exclude_from_training"])
    assert missed > disputed
    assert disputed / positives < MAX_FLIPPED_NORMAL_SHARE


def test_label_noise_rates_are_configurable() -> None:
    clean = generate(
        SyntheticConfig(seed=3, label_noise_fn_rate=0.0, label_noise_fp_rate=0.0, **SMALL)
    )
    assert not any(t.get("label_noise") for t in clean.transactions)
    only_missed = generate(
        SyntheticConfig(seed=3, label_noise_fn_rate=0.5, label_noise_fp_rate=0.0, **SMALL)
    )
    flipped = [t for t in only_missed.transactions if t.get("label_noise")]
    assert flipped
    assert all(t["typology"] != "normal" and t["label"] == 0 for t in flipped)


def test_identifier_fields_alone_predict_fraud_at_chance(data) -> None:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold, cross_val_predict

    rows = [t for t in data.transactions if not t["exclude_from_training"]]
    x = np.asarray([_id_features(t) for t in rows])
    y = np.asarray([int(t["typology"] != "normal") for t in rows])
    # Folds grouped by device: a card-testing burst reuses one device 6-16 times,
    # and memorising that device's digit count is entity identity, not a format leak.
    proba = cross_val_predict(
        HistGradientBoostingClassifier(max_iter=60, random_state=0),
        x,
        y,
        cv=GroupKFold(3),
        groups=[t["device_id"] for t in rows],
        method="predict_proba",
    )[:, 1]
    assert roc_auc_score(y, proba) < CHANCE_AUC


@pytest.mark.slow
def test_no_single_feature_dominates_the_model(data, tmp_path) -> None:
    cfg = TrainingConfig(
        seed=5, version="fp_test", models_dir=tmp_path, num_boost_round=120, iforest_estimators=40
    )
    report = train(data.customers, data.transactions, cfg)
    import json

    meta = json.loads((report.path / "metadata.json").read_text(encoding="utf-8"))
    importance = meta["feature_importance"]
    top, share = max(importance.items(), key=lambda kv: kv[1])
    assert share <= MAX_FEATURE_SHARE, (top, share)
    device_share = importance.get("is_new_device", 0) + importance.get("device_age_d", 0)
    assert device_share <= MAX_FEATURE_SHARE


def _text_network_features(tx: dict, purposes: list[str]) -> list[float]:
    """Purpose, IP prefix and beneficiary-name shape (the audit's L9 probe)."""
    octets = [*str(tx.get("ip_address") or "0.0.0.0").split("."), "0", "0"][:2]
    name = str(tx.get("beneficiary_name") or "")
    return [
        float(purposes.index(tx["purpose"])),
        float(octets[0]),
        float(octets[1]),
        float(len(name.split())),
        float(len(name)),
        float(any(ch.isdigit() for ch in name)),
    ]


def test_purpose_ip_and_name_alone_are_weak_predictors(data) -> None:
    from sklearn.metrics import average_precision_score
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from sklearn.tree import DecisionTreeClassifier

    rows = [t for t in data.transactions if not t["exclude_from_training"]]
    purposes = sorted({t["purpose"] for t in rows})
    x = np.asarray([_text_network_features(t, purposes) for t in rows])
    y = np.asarray([int(t["typology"] != "normal") for t in rows])
    proba = cross_val_predict(
        DecisionTreeClassifier(max_depth=4, random_state=0),
        x,
        y,
        cv=StratifiedKFold(3, shuffle=True, random_state=0),
        method="predict_proba",
    )[:, 1]
    ap = average_precision_score(y, proba)
    assert ap < MAX_TEXT_NETWORK_AP, ap
    assert ap / y.mean() < MAX_TEXT_NETWORK_LIFT, (ap, y.mean())
