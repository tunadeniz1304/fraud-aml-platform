"""ML building blocks: fast IForest, ECOD, TreeSHAP parity, calibration, registry, metrics."""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.ml import metrics as M
from app.ml.anomaly import (
    AnomalyDetector,
    ECODScorer,
    FastIsolationForest,
    QuantileCalibrator,
)
from app.ml.gbm import GBMModel
from app.ml.registry import ModelRegistry, RegistryError, load_bundle, save_bundle
from app.ml.stacker import Stacker


@pytest.fixture(scope="module")
def data() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(7)
    n = 3000
    x = rng.normal(size=(n, 6))
    x[:, 4] = (rng.random(n) < 0.1).astype(float)  # binary column
    logits = 2.2 * x[:, 0] - 1.5 * x[:, 1] + 2.0 * x[:, 4] - 4.0
    y = (rng.random(n) < 1 / (1 + np.exp(-logits))).astype(int)
    return {"x": x, "y": y, "t": rng.normal(size=(300, 6)) * 1.7}


@pytest.fixture(scope="module")
def gbm(data) -> GBMModel:
    x, y = data["x"], data["y"]
    names = [f"f{i}" for i in range(x.shape[1])]
    return GBMModel.train(x[:2000], y[:2000], x[2000:], y[2000:], names, num_boost_round=60)


class TestIsolationForest:
    def test_fast_scorer_equals_sklearn(self, data, tmp_path):
        from sklearn.ensemble import IsolationForest

        model = IsolationForest(n_estimators=50, max_samples=128, random_state=3).fit(data["x"])
        fast = FastIsolationForest.from_sklearn(model)
        ref = model.score_samples(data["t"])
        mine = np.array([fast.raw_score(r) for r in data["t"].tolist()])
        np.testing.assert_allclose(mine, ref, atol=1e-12)
        fast.save(tmp_path / "if.npz")
        loaded = FastIsolationForest.load(tmp_path / "if.npz")
        row = data["t"][0].tolist()
        assert loaded.raw_score(row) == pytest.approx(fast.raw_score(row))
        assert fast.anomaly(row) == -fast.raw_score(row)


class TestECOD:
    def test_rank_agreement_with_pyod(self, data, tmp_path):
        from pyod.models.ecod import ECOD
        from scipy.stats import spearmanr

        ours = ECODScorer.fit(data["x"])
        ref = ECOD().fit(data["x"]).decision_function(data["t"])
        rho = spearmanr(ref, [ours.score(r) for r in data["t"].tolist()]).statistic
        assert rho > 0.9
        ours.save(tmp_path / "e.npz")
        loaded = ECODScorer.load(tmp_path / "e.npz")
        assert loaded.score(data["t"][1].tolist()) == pytest.approx(
            ours.score(data["t"][1].tolist())
        )

    def test_out_of_range_values_are_finite(self, data):
        ours = ECODScorer.fit(data["x"])
        assert np.isfinite(ours.score([1e9] * 6)) and np.isfinite(ours.score([-1e9] * 6))


class TestCalibrationAndEnsemble:
    def test_quantile_calibrator(self):
        cal = QuantileCalibrator.fit(list(range(101)), points=11)
        assert cal(-5) == 0.0 and cal(500) == 1.0
        assert cal(50) == pytest.approx(0.5)
        assert cal(55) == pytest.approx(0.55)
        assert QuantileCalibrator([1.0, 1.0, 2.0])(1.0) == 0.0

    def test_detector_average(self, data):
        from sklearn.ensemble import IsolationForest

        iso = FastIsolationForest.from_sklearn(
            IsolationForest(n_estimators=20, random_state=0).fit(data["x"])
        )
        ecod = ECODScorer.fit(data["x"])
        det = AnomalyDetector(
            iso,
            ecod,
            QuantileCalibrator.fit([iso.anomaly(r) for r in data["x"][:500].tolist()]),
            QuantileCalibrator.fit([ecod.score(r) for r in data["x"][:500].tolist()]),
        )
        normal = det.score(np.zeros(6).tolist())
        weird = det.score([8, -8, 8, -8, 1, 8])
        assert 0 <= normal < weird <= 1


class TestGBM:
    def test_contributions_equal_shap_tree_explainer(self, data, gbm):
        import shap

        x = data["t"][:50]
        ours = gbm.contributions(x)
        explainer = shap.TreeExplainer(gbm.booster)
        ref = np.asarray(explainer.shap_values(x))
        if ref.ndim == 3:  # older shap returns [neg, pos]
            ref = ref[-1]
        np.testing.assert_allclose(ours[:, :-1], ref, atol=1e-6)
        np.testing.assert_allclose(ours[:, -1], explainer.expected_value, atol=1e-6)

    def test_explain_probability_matches_predict(self, data, gbm, tmp_path):
        row = data["t"][3].tolist()
        exp = gbm.explain(row)
        assert exp.probability == pytest.approx(gbm.predict(np.asarray([row]))[0])
        assert set(exp.contributions) == set(gbm.feature_names)
        assert sum(gbm.feature_importance().values()) == pytest.approx(1.0, abs=1e-3)
        gbm.save(tmp_path / "m.txt")
        again = GBMModel.load(tmp_path / "m.txt")
        assert again.explain(row).probability == pytest.approx(exp.probability)


class TestRegistry:
    def _bundle(self, root, version, gbm, data):
        from sklearn.ensemble import IsolationForest

        iso = FastIsolationForest.from_sklearn(
            IsolationForest(n_estimators=10, random_state=0).fit(data["x"])
        )
        ecod = ECODScorer.fit(data["x"][:300])
        save_bundle(
            root / version,
            gbm=gbm,
            stacker=Stacker((0.3, 1.0, 0.1), 0.0),
            iforest=iso,
            ecod=ecod,
            cal_iforest=QuantileCalibrator([0.0, 0.5, 1.0]),
            cal_ecod=QuantileCalibrator([0.0, 5.0, 50.0]),
            metadata={"version": version},
        )

    def test_roles_promote_and_load(self, tmp_path, gbm, data):
        registry = ModelRegistry(tmp_path)
        assert registry.read()["champion"] is None and registry.load_role("champion") is None
        self._bundle(tmp_path, "m1", gbm, data)
        self._bundle(tmp_path, "m2", gbm, data)
        registry.register("m1", metrics={"pr_auc": 0.9}, status="champion")
        registry.register("m2", metrics={"pr_auc": 0.95})
        assert registry.champion_version() == "m1" and registry.challenger_version() == "m2"
        with pytest.raises(RegistryError):
            registry.set_status("m1", "challenger")
        registry.promote("m2")
        data_ = registry.read()
        assert data_["champion"] == "m2" and data_["challenger"] is None
        assert data_["models"]["m1"]["status"] == "archived"
        bundle = registry.load_role("champion")
        assert bundle is not None and bundle.version == "m2"
        score = bundle.score({"f0": 3.0, "f4": 1.0})
        assert 0 <= score.ml <= 1 and score.anomaly is not None and score.iforest is not None
        assert load_bundle(tmp_path / "m2") is load_bundle(tmp_path / "m2")  # cached
        registry.set_status("m2", "archived")
        assert registry.champion_version() is None
        assert [m["version"] for m in registry.models()] == ["m1", "m2"]

    def test_errors(self, tmp_path):
        registry = ModelRegistry(tmp_path)
        with pytest.raises(RegistryError):
            registry.register("nope", metrics={})
        with pytest.raises(RegistryError):
            registry.set_status("nope", "champion")
        (tmp_path / "x").mkdir()
        (tmp_path / "x" / "model.txt").write_text("x")
        with pytest.raises(RegistryError):
            registry.register("x", metrics={}, status="deleted")
        registry.file.write_text(json.dumps({"models": {"x": {"status": "archived"}}}))
        with pytest.raises(RegistryError):
            registry.set_status("x", "weird")


class TestMetrics:
    def test_ranking_metrics(self):
        y = [0, 0, 0, 0, 1, 1]
        s = [0.1, 0.2, 0.3, 0.4, 0.8, 0.9]
        assert M.pr_auc(y, s) == pytest.approx(1.0)
        assert M.roc_auc(y, s) == pytest.approx(1.0)
        assert M.recall_at_fpr(y, s, 0.25) == 1.0
        assert M.recall_at_fpr([0, 0], [0.1, 0.2]) == 0.0
        budget = M.budget_metrics(y, s, [1, 1, 1, 1, 100, 300], budget=0.2)
        assert budget["alerts"] == 1 and budget["precision"] == 1.0
        assert budget["cost_weighted_recall"] == pytest.approx(0.75)
        report = M.evaluate(y, s, [1] * 6)
        assert report["positives"] == 2 and report["budget_1pct"]["alerts"] == 1

    def test_psi(self):
        rng = np.random.default_rng(0)
        ref = rng.normal(size=5000).tolist()
        same = rng.normal(size=5000).tolist()
        shifted = (rng.normal(size=5000) + 1.0).tolist()
        assert M.psi(ref, same) < 0.02
        assert M.psi(ref, shifted) > 0.25
        edges = M.reference_bins(ref)
        share = M.distribution(ref, edges)
        assert sum(share) == pytest.approx(1.0)
        assert M.psi_from_distribution(share, shifted, edges) > 0.25
