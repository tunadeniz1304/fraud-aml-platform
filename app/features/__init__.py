"""Streaming feature store (P0.4): sliding windows, EWMA profiles, registry."""

from __future__ import annotations

from app.features.definitions import FEATURES, compute_features, describe, feature_names
from app.features.extractor import CustomerDirectory, Extraction, FeatureExtractor
from app.features.store import MemoryFeatureStore, RedisFeatureStore
from app.features.types import CustomerStatic, ProfileState, TxView

__all__ = [
    "FEATURES",
    "CustomerDirectory",
    "CustomerStatic",
    "Extraction",
    "FeatureExtractor",
    "MemoryFeatureStore",
    "ProfileState",
    "RedisFeatureStore",
    "TxView",
    "compute_features",
    "describe",
    "feature_names",
]
