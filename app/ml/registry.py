"""Model registry: versioned model bundles with champion / challenger status.

Layout::

    models/
      registry.json                 {"champion": "fraud_gbm_v1", "challenger": null, ...}
      fraud_gbm_v1/
        model.txt                   LightGBM booster
        iforest.npz, ecod.npz       anomaly detectors (hot-path arrays)
        calibration.json            anomaly percentile tables
        stacker.json                logistic stacker (rule, ml, anomaly)
        metadata.json               features, metrics, PSI reference bins, data manifest
        model_card.md               human-readable model card

Only the internal feature-store features are model inputs; graph / APP /
consortium signals enter at the policy layer.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.ml.anomaly import AnomalyDetector, ECODScorer, FastIsolationForest, QuantileCalibrator
from app.ml.gbm import GBMModel
from app.ml.stacker import Stacker

STATUSES = ("champion", "challenger", "archived")


class RegistryError(ValueError):
    pass


@dataclass
class ModelScore:
    ml: float
    anomaly: float | None
    contributions: dict[str, float]
    iforest: float | None = None
    ecod: float | None = None


@dataclass
class ModelBundle:
    version: str
    path: Path
    gbm: GBMModel
    stacker: Stacker
    anomaly: AnomalyDetector | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def feature_names(self) -> list[str]:
        return self.gbm.feature_names

    def vector(self, features: Mapping[str, float]) -> list[float]:
        return [float(features.get(n, 0.0)) for n in self.feature_names]

    def score(self, features: Mapping[str, float], *, explain: bool = True) -> ModelScore:
        """Probability + anomaly; TreeSHAP contributions only when ``explain``."""
        row = self.vector(features)
        contributions: dict[str, float] = {}
        if explain:
            explanation = self.gbm.explain(row)
            probability, contributions = explanation.probability, explanation.contributions
        else:
            probability = self.gbm.probability(row)
        anomaly = iforest = ecod = None
        if self.anomaly is not None:
            iforest, ecod = self.anomaly.components(row)
            anomaly = 0.5 * (iforest + ecod)
        return ModelScore(probability, anomaly, contributions, iforest, ecod)

    def explain(self, features: Mapping[str, float]) -> dict[str, float]:
        return self.gbm.explain(self.vector(features)).contributions


def save_bundle(
    directory: Path,
    *,
    gbm: GBMModel,
    stacker: Stacker,
    iforest: FastIsolationForest,
    ecod: ECODScorer,
    cal_iforest: QuantileCalibrator,
    cal_ecod: QuantileCalibrator,
    metadata: dict[str, Any],
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    gbm.save(directory / "model.txt")
    iforest.save(directory / "iforest.npz")
    ecod.save(directory / "ecod.npz")
    (directory / "calibration.json").write_text(
        json.dumps({"iforest": cal_iforest.to_list(), "ecod": cal_ecod.to_list()}),
        encoding="utf-8",
    )
    (directory / "stacker.json").write_text(json.dumps(stacker.to_dict(), indent=1), "utf-8")
    (directory / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def load_bundle(directory: Path, version: str | None = None) -> ModelBundle:
    stamp = (directory / "model.txt").stat().st_mtime_ns
    return _load_bundle_cached(str(directory), version or directory.name, stamp)


@lru_cache(maxsize=8)
def _load_bundle_cached(directory: str, version: str, _stamp: int) -> ModelBundle:
    root = Path(directory)
    gbm = GBMModel.load(root / "model.txt")
    stacker = Stacker.from_dict(json.loads((root / "stacker.json").read_text("utf-8")))
    anomaly = None
    if (root / "iforest.npz").is_file() and (root / "ecod.npz").is_file():
        cal = json.loads((root / "calibration.json").read_text("utf-8"))
        anomaly = AnomalyDetector(
            FastIsolationForest.load(root / "iforest.npz"),
            ECODScorer.load(root / "ecod.npz"),
            QuantileCalibrator(cal["iforest"]),
            QuantileCalibrator(cal["ecod"]),
        )
    meta_path = root / "metadata.json"
    metadata = json.loads(meta_path.read_text("utf-8")) if meta_path.is_file() else {}
    return ModelBundle(version, root, gbm, stacker, anomaly, metadata)


class ModelRegistry:
    """``models/registry.json`` reader/writer (thread-safe, atomic writes)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.file = root / "registry.json"
        self._lock = threading.Lock()

    def read(self) -> dict[str, Any]:
        if not self.file.is_file():
            return {"champion": None, "challenger": None, "models": {}}
        data: dict[str, Any] = json.loads(self.file.read_text(encoding="utf-8"))
        data.setdefault("models", {})
        data.setdefault("champion", None)
        data.setdefault("challenger", None)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)

    def models(self) -> list[dict[str, Any]]:
        data = self.read()
        return [{"version": v, **info} for v, info in sorted(data["models"].items())]

    def champion_version(self) -> str | None:
        return self.read()["champion"]

    def challenger_version(self) -> str | None:
        return self.read()["challenger"]

    def register(
        self, version: str, *, metrics: dict[str, Any], status: str = "challenger"
    ) -> dict[str, Any]:
        if status not in STATUSES:
            raise RegistryError(f"geçersiz model durumu: {status}")
        if not (self.root / version / "model.txt").is_file():
            raise RegistryError(f"model dosyası bulunamadı: {version}")
        with self._lock:
            data = self.read()
            data["models"][version] = {
                "path": version,
                "status": "archived",
                "metrics": metrics,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            self._set_status(data, version, status)
            self._write(data)
            return data

    def _set_status(self, data: dict[str, Any], version: str, status: str) -> None:
        models = data["models"]
        if status == "champion":
            old = data.get("champion")
            if old and old != version and old in models:
                models[old]["status"] = "archived"
            if data.get("challenger") == version:
                data["challenger"] = None
            data["champion"] = version
            models[version]["promoted_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        elif status == "challenger":
            old = data.get("challenger")
            if old and old != version and old in models:
                models[old]["status"] = "archived"
            if data.get("champion") == version:
                raise RegistryError("champion model challenger yapılamaz")
            data["challenger"] = version
        else:
            for role in ("champion", "challenger"):
                if data.get(role) == version:
                    data[role] = None
        models[version]["status"] = status

    def set_status(self, version: str, status: str) -> dict[str, Any]:
        with self._lock:
            data = self.read()
            if version not in data["models"]:
                raise RegistryError(f"model bulunamadı: {version}")
            if status not in STATUSES:
                raise RegistryError(f"geçersiz model durumu: {status}")
            self._set_status(data, version, status)
            self._write(data)
            return data

    def restore(self, data: dict[str, Any]) -> None:
        """Write back a snapshot taken with :meth:`read` (undoes a promotion
        whose approval transaction did not commit, A5)."""
        with self._lock:
            self._write(data)

    def promote(self, version: str) -> dict[str, Any]:
        return self.set_status(version, "champion")

    def load(self, version: str) -> ModelBundle:
        return load_bundle(self.root / version, version)

    def load_role(self, role: str) -> ModelBundle | None:
        version = self.read().get(role)
        if not version or not (self.root / version / "model.txt").is_file():
            return None
        return self.load(version)
