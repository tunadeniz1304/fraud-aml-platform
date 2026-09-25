"""Offline training pipeline (P0.7): backfill → split → train → evaluate → register.

1. **Backfill** — the labelled stream is replayed chronologically through the
   *same* :class:`~app.scoring.engine.ScoringEngine` (feature store → rules →
   policy) the online scorer uses. The profile learns through the shared
   :func:`~app.features.learning.should_learn` rule with the step-up outcome
   simulated from the label, exactly like production learns from the
   ``step-up-result`` feedback — see :func:`live_replay_features` and the
   parity test. The replay has no champion model yet (rules + policy decide),
   so events a later model would move across ALLOW are the residual skew.
2. **Time-based split** 70 / 15 / 15 — the model is always evaluated on the
   future, never on shuffled rows.
3. **Models** — LightGBM (≤300 trees, early stopping), IsolationForest on clean
   training rows (served by :class:`FastIsolationForest`), ECOD, then a
   logistic **stacker** over (rule, ml, anomaly) fitted on the validation split.
4. **Evaluation** on the test split: PR-AUC, ROC-AUC, recall@1% FPR and
   cost-weighted recall under a 1% alert budget, per component and hybrid.
5. **Artifacts** — bundle under ``models/<version>/`` + ``model_card.md`` and a
   registry entry.

Sanctions hits are excluded from ML (the screener owns them).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.features.definitions import FEATURES, describe
from app.features.extractor import CustomerDirectory, FeatureExtractor
from app.features.learning import simulated_feedback
from app.features.store import MemoryFeatureStore
from app.ml import metrics as M
from app.ml.anomaly import ECODScorer, FastIsolationForest, QuantileCalibrator
from app.ml.gbm import GBMModel
from app.ml.registry import ModelRegistry, save_bundle
from app.ml.stacker import Stacker
from app.scoring.policy import Thresholds, default_thresholds
from app.scoring.rules import RuleSet, load_ruleset

logger = logging.getLogger("fraud.training")


@dataclass
class TrainingConfig:
    seed: int = 42
    version: str = "fraud_gbm_v1"
    models_dir: Path = Path("models")
    status: str = "champion"
    train_share: float = 0.70
    valid_share: float = 0.15
    num_boost_round: int = 300
    iforest_estimators: int = 100
    ecod_reference_rows: int = 3000
    backtest_path: Path | None = None
    data_manifest: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackfillRow:
    transaction_id: str
    ts: str
    customer_id: str
    amount_try: float
    label: int
    typology: str
    exclude: bool
    features: list[float]
    rule_score: float


@dataclass
class TrainingReport:
    version: str
    path: Path
    metrics: dict[str, Any]
    seconds: float
    rows: int

    def headline(self) -> str:
        h = self.metrics["test"]["hybrid"]
        return (
            f"{self.version}: PR-AUC={h['pr_auc']:.3f} ROC-AUC={h['roc_auc']:.3f} "
            f"recall@1%FPR={h['recall_at_1pct_fpr']:.3f} ({self.seconds:.1f} sn)"
        )


def model_feature_names() -> list[str]:
    """ML inputs = internal feature-store features only (stable registry order)."""
    return list(FEATURES)


def replay_engine(customers: Sequence[dict[str, Any]], ruleset: RuleSet) -> Any:
    """Model-free engine used by the backfill and the parity check."""
    from app.core.sanctions import SanctionScreener
    from app.scoring.engine import ScoringEngine
    from app.scoring.policy import PolicyEngine

    extractor = FeatureExtractor(MemoryFeatureStore(), CustomerDirectory(customers))
    return ScoringEngine(
        extractor, ruleset=ruleset, policy=PolicyEngine(None), sanctions=SanctionScreener()
    )


def _ordered(transactions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(transactions, key=lambda t: (t["ts"], t["transaction_id"]))


async def backfill(
    customers: Sequence[dict[str, Any]],
    transactions: Sequence[dict[str, Any]],
    ruleset: RuleSet,
) -> list[BackfillRow]:
    names = model_feature_names()
    engine = replay_engine(customers, ruleset)
    rows: list[BackfillRow] = []
    for tx in _ordered(transactions):
        result = await engine.score(tx)
        feats = result.extraction.features
        label = int(tx.get("label", 0))
        rows.append(
            BackfillRow(
                transaction_id=str(tx["transaction_id"]),
                ts=str(tx["ts"]),
                customer_id=str(tx["customer_id"]),
                amount_try=result.amount_try,
                label=label,
                typology=str(tx.get("typology", "normal")),
                exclude=bool(tx.get("exclude_from_training", False)),
                features=[feats[n] for n in names],
                rule_score=result.rules.score if result.rules else ruleset.score_only(feats),
            )
        )
        await engine.commit(result, feedback=simulated_feedback(result.decision, label))
    return rows


async def live_replay_features(
    customers: Sequence[dict[str, Any]],
    transactions: Sequence[dict[str, Any]],
    ruleset: RuleSet,
) -> list[list[float]]:
    """Replay the stream the way production sees it (parity check).

    Each event is scored and committed without feedback; the step-up outcome
    arrives afterwards through :meth:`ScoringEngine.apply_feedback`, as the
    ``POST /api/transactions/{id}/step-up-result`` endpoint delivers it.
    """
    names = model_feature_names()
    engine = replay_engine(customers, ruleset)
    out: list[list[float]] = []
    for tx in _ordered(transactions):
        result = await engine.score(tx)
        out.append([result.extraction.features[n] for n in names])
        await engine.commit(result)
        feedback = simulated_feedback(result.decision, int(tx.get("label", 0)))
        if feedback is not None:
            await engine.apply_feedback(result.transaction_id, feedback)
    return out


def _split(rows: list[BackfillRow], cfg: TrainingConfig) -> tuple[list[BackfillRow], ...]:
    usable = [r for r in rows if not r.exclude]
    n = len(usable)
    a = int(n * cfg.train_share)
    b = int(n * (cfg.train_share + cfg.valid_share))
    return usable[:a], usable[a:b], usable[b:]


def _xy(rows: Sequence[BackfillRow]) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.asarray([r.features for r in rows], dtype=float),
        np.asarray([r.label for r in rows], dtype=int),
    )


def _typology_outcomes(
    rows: Sequence[BackfillRow], scores: Sequence[float], thresholds: Thresholds
) -> dict[str, dict[str, int]]:
    out: dict[str, Counter[str]] = {}
    for row, score in zip(rows, scores, strict=True):
        out.setdefault(row.typology, Counter())[thresholds.action(score)] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}


def train_from_rows(rows: list[BackfillRow], cfg: TrainingConfig) -> TrainingReport:
    from sklearn.ensemble import IsolationForest

    started = time.perf_counter()
    names = model_feature_names()
    train, valid, test = _split(rows, cfg)
    x_tr, y_tr = _xy(train)
    x_va, y_va = _xy(valid)
    x_te, y_te = _xy(test)
    if y_tr.sum() == 0 or y_va.sum() == 0 or y_te.sum() == 0:
        raise ValueError("her bölümde en az bir fraud örneği olmalı (veri çok küçük)")

    gbm = GBMModel.train(
        x_tr, y_tr, x_va, y_va, names, seed=cfg.seed, num_boost_round=cfg.num_boost_round
    )

    clean = x_tr[y_tr == 0]
    iso = IsolationForest(
        n_estimators=cfg.iforest_estimators, max_samples=256, random_state=cfg.seed
    ).fit(clean)
    iforest = FastIsolationForest.from_sklearn(iso)
    rng = np.random.default_rng(cfg.seed)
    ref_idx = rng.choice(len(clean), size=min(cfg.ecod_reference_rows, len(clean)), replace=False)
    ecod = ECODScorer.fit(clean[np.sort(ref_idx)])

    va_clean = x_va[y_va == 0]
    cal_if = QuantileCalibrator.fit([iforest.anomaly(r) for r in va_clean.tolist()])
    cal_ec = QuantileCalibrator.fit([ecod.score(r) for r in va_clean.tolist()])

    def anomaly_scores(x: np.ndarray) -> list[float]:
        return [0.5 * (cal_if(iforest.anomaly(r)) + cal_ec(ecod.score(r))) for r in x.tolist()]

    ml_va = gbm.predict(x_va).tolist()
    an_va = anomaly_scores(x_va)
    rule_va = [r.rule_score for r in valid]
    stacker = Stacker.fit(
        list(zip(rule_va, ml_va, an_va, strict=True)), y_va.tolist(), seed=cfg.seed
    )

    ml_te = gbm.predict(x_te).tolist()
    an_te = anomaly_scores(x_te)
    rule_te = [r.rule_score for r in test]
    hybrid_te = [stacker.predict(r, m, a) for r, m, a in zip(rule_te, ml_te, an_te, strict=True)]
    amounts_te = [r.amount_try for r in test]
    thresholds = default_thresholds()
    test_metrics = {
        "rule": M.evaluate(y_te.tolist(), rule_te, amounts_te),
        "ml": M.evaluate(y_te.tolist(), ml_te, amounts_te),
        "anomaly": M.evaluate(y_te.tolist(), an_te, amounts_te),
        "hybrid": M.evaluate(y_te.tolist(), hybrid_te, amounts_te),
        "decisions_by_typology": _typology_outcomes(test, hybrid_te, thresholds),
    }
    hybrid_va = [stacker.predict(r, m, a) for r, m, a in zip(rule_va, ml_va, an_va, strict=True)]
    importance = gbm.feature_importance()
    top_features = sorted(importance, key=lambda k: -importance[k])[:10]
    psi_reference: dict[str, Any] = {}
    for name, values in [("score", hybrid_va)] + [
        (f, x_va[:, names.index(f)].tolist()) for f in top_features
    ]:
        edges = M.reference_bins(values)
        psi_reference[name] = {"edges": edges, "share": M.distribution(values, edges)}

    elapsed = time.perf_counter() - started
    metrics = {
        "test": test_metrics,
        "valid_pr_auc_ml": round(M.pr_auc(y_va.tolist(), ml_va), 4),
        "split": {
            "train": [len(train), int(y_tr.sum())],
            "valid": [len(valid), int(y_va.sum())],
            "test": [len(test), int(y_te.sum())],
            "test_period": [test[0].ts, test[-1].ts],
        },
        "trees": gbm.booster.num_trees(),
    }
    metadata = {
        "version": cfg.version,
        "created_with_seed": cfg.seed,
        "features": names,
        "feature_descriptions": {n: describe(n) for n in names},
        "feature_importance": importance,
        "stacker": stacker.to_dict(),
        "thresholds": thresholds.as_dict(),
        "metrics": metrics,
        "psi_reference": psi_reference,
        "data": cfg.data_manifest,
        "train_seconds": round(elapsed, 2),
    }
    out = cfg.models_dir / cfg.version
    save_bundle(
        out,
        gbm=gbm,
        stacker=stacker,
        iforest=iforest,
        ecod=ecod,
        cal_iforest=cal_if,
        cal_ecod=cal_ec,
        metadata=metadata,
    )
    (out / "model_card.md").write_text(render_model_card(metadata), encoding="utf-8")
    registry = ModelRegistry(cfg.models_dir)
    registry.register(cfg.version, metrics=_summary_metrics(metrics), status=cfg.status)
    if cfg.backtest_path is not None:
        write_backtest(cfg.backtest_path, test, names)
    return TrainingReport(cfg.version, out, metrics, elapsed, len(rows))


def _summary_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    h = metrics["test"]["hybrid"]
    return {
        "pr_auc": h["pr_auc"],
        "roc_auc": h["roc_auc"],
        "recall_at_1pct_fpr": h["recall_at_1pct_fpr"],
        "cost_weighted_recall": h["budget_1pct"]["cost_weighted_recall"],
        "ml_pr_auc": metrics["test"]["ml"]["pr_auc"],
    }


def write_backtest(path: Path, rows: Sequence[BackfillRow], names: Sequence[str]) -> None:
    """Held-out features + labels for rule backtesting (rule studio)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            feats = {n: round(v, 6) for n, v in zip(names, r.features, strict=True)}
            fh.write(json.dumps({"id": r.transaction_id, "y": r.label, "f": feats}) + "\n")


def train(
    customers: Sequence[dict[str, Any]],
    transactions: Sequence[dict[str, Any]],
    cfg: TrainingConfig,
    *,
    ruleset: RuleSet | None = None,
) -> TrainingReport:
    started = time.perf_counter()
    rows = asyncio.run(backfill(customers, transactions, ruleset or load_ruleset()))
    logger.info("Backfill: %d satır %.1f sn", len(rows), time.perf_counter() - started)
    report = train_from_rows(rows, cfg)
    report.seconds = time.perf_counter() - started
    return report


# --- model card -----------------------------------------------------------------------
def _pct(v: float) -> str:
    return f"%{v * 100:.1f}".replace(".", ",")


def render_model_card(meta: dict[str, Any]) -> str:
    m = meta["metrics"]
    t = m["test"]
    rows = []
    for comp, label in (
        ("rule", "Kural motoru"),
        ("ml", "LightGBM"),
        ("anomaly", "Anomali (IForest+ECOD)"),
        ("hybrid", "**Hibrit (stacker)**"),
    ):
        r = t[comp]
        budget = r["budget_1pct"]
        rows.append(
            f"| {label} | {r['pr_auc']:.3f} | {r['roc_auc']:.3f} | {r['recall_at_1pct_fpr']:.3f} "
            f"| {budget['precision']:.3f} | {budget['cost_weighted_recall']:.3f} |"
        )
    typ_rows = []
    for typ, counts in t["decisions_by_typology"].items():
        total = sum(counts.values())
        cells = " | ".join(str(counts.get(a, 0)) for a in ("ALLOW", "STEP_UP", "HOLD", "BLOCK"))
        typ_rows.append(f"| {typ} | {total} | {cells} |")
    imp = meta["feature_importance"]
    top = sorted(imp, key=lambda k: -imp[k])[:12]
    imp_rows = [f"| `{f}` | {meta['feature_descriptions'][f]} | {imp[f]:.3f} |" for f in top]
    split = m["split"]
    data = meta.get("data") or {}
    typologies = ", ".join(f"{k}: {v}" for k, v in (data.get("typologies") or {}).items())
    st = meta["stacker"]
    lines = [
        f"# Model kartı — `{meta['version']}`",
        "",
        "## Amaç ve kapsam",
        "Gerçek zamanlı giden transferlerde (FAST/EFT/havale/kart) dolandırıcılık olasılığı "
        "üretir. Karar **vermez**: çıktısı kural skoru ve anomali skoru ile birlikte lojistik "
        "stacker'a, oradan politika katmanına (ALLOW / STEP_UP / HOLD / BLOCK) gider. Yaptırım "
        "taraması, hesap durumu ve kural aksiyon tabanları deterministik override'lardır.",
        "",
        "## Veri",
        f"- Sentetik, seed'li Türk bankacılığı verisi (seed={meta['created_with_seed']}); "
        f"{data.get('customers', '?')} müşteri, {data.get('transactions', '?')} işlem, "
        f"fraud oranı {data.get('fraud_rate', '?')}.",
        f"- Tipolojiler: {typologies or 'n/a'} (yaptırım isabetleri ML eğitiminden hariç).",
        f"- Zaman bazlı bölme 70/15/15 — eğitim {split['train'][0]} ({split['train'][1]} fraud), "
        f"doğrulama {split['valid'][0]} ({split['valid'][1]}), test {split['test'][0]} "
        f"({split['test'][1]}); test dönemi {split['test_period'][0]} → {split['test_period'][1]}.",
        "- Feature'lar online skorlayıcıyla **aynı** motorla (feature store → kurallar → "
        "politika) kronolojik replay ile üretildi; profil öğrenmesi canlı sistemle aynı "
        "`should_learn` kuralını kullanır (parite testi: `test_a3_*`). Replay'de champion "
        "model yoktur; modelin ALLOW sınırını değiştirdiği olaylar kalan kaymadır.",
        "",
        "## Test metrikleri",
        "| Bileşen | PR-AUC | ROC-AUC | Recall @ %1 FPR | Kesinlik @ %1 bütçe | "
        "Maliyet ağırlıklı recall @ %1 bütçe |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
        "Maliyet ağırlıklı recall: işlemlerin en riskli %1'i alert olduğunda yakalanan fraud "
        "**tutarının** toplam fraud tutarına oranı.",
        "",
        "## Tipoloji bazında politika sonuçları (test, harici sinyaller hariç)",
        "| Tipoloji | Adet | ALLOW | STEP_UP | HOLD | BLOCK |",
        "|---|---|---|---|---|---|",
        *typ_rows,
        "",
        f"Eşikler: {meta['thresholds']}.",
        "",
        "## En etkili feature'lar (LightGBM gain)",
        "| Feature | Açıklama | Pay |",
        "|---|---|---|",
        *imp_rows,
        "",
        "## Açıklanabilirlik",
        "Her kararda LightGBM `pred_contrib` (TreeSHAP; `shap.TreeExplainer` ile birebir aynı, "
        "testli) katkıları feature grubuna göre toplanıp `ML_*` reason code'larına eşlenir; "
        "kural isabetleri Türkçe şablonlu reason code üretir.",
        "",
        "## Stacker",
        f"Girdi: logit(kural), logit(ML), logit(anomali); katsayılar {st['coef']}, "
        f"kesişim {st['intercept']:.3f}. Katsayılar ≥ 0.05 ile tabanlanır (monotonluk: hiçbir "
        "bileşen riski düşüremez).",
        "",
        "## Sınırlamalar ve riskler",
        "- Sentetik veriyle eğitildi; gerçek dağılımlarda yeniden eğitim ve kalibrasyon şart.",
        "- APP dolandırıcılığında cihaz/konum tanıdık olduğundan model sosyal mühendislik "
        "sinyallerine (görüşme, uzaktan erişim, metin) dayanır; bu sinyaller yoksa kaçırma "
        "olasılığı artar — graf ve CoP sinyalleri politika katmanında bunu telafi eder.",
        "- Yaşlı/kırılgan müşteri bayrağı riski artırır; amaç koruyucu friction'dır (uyarı, "
        "bekletme), ret değil. Adillik izlemesi için karar oranları segment bazında izlenmeli.",
        "- Drift: skor ve en etkili 10 feature için PSI referans dağılımları metadata'dadır.",
        "",
        f"Eğitim süresi: {meta['train_seconds']} sn · ağaç sayısı: {m['trees']}.",
        "",
    ]
    return "\n".join(lines)
