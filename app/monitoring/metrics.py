"""Prometheus metrics (single registry for the whole process).

Exposed on ``GET /metrics``. Names follow the ``fraud_`` prefix convention.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

_LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0)

# --- transactions / scoring --------------------------------------------------
TX_TOTAL = Counter(
    "fraud_transactions_total", "İşlenen işlem sayısı", ["source"], registry=REGISTRY
)
TX_REJECTED = Counter(
    "fraud_transactions_rejected_total", "Doğrulamayı geçemeyen işlemler", registry=REGISTRY
)
SCORE_LATENCY = Histogram(
    "fraud_scoring_latency_seconds",
    "Senkron skor yolu gecikmesi (LLM hariç)",
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
DECISIONS = Counter("fraud_decisions_total", "Karar dağılımı", ["decision"], registry=REGISTRY)
ALERTS = Counter("fraud_alerts_total", "Üretilen alert sayısı", ["alert_type"], registry=REGISTRY)
RISK_SCORE = Histogram(
    "fraud_risk_score",
    "Politika risk skoru dağılımı",
    buckets=tuple(i / 20 for i in range(1, 21)),
    registry=REGISTRY,
)
MODEL_SCORE = Histogram(
    "fraud_model_score",
    "Model olasılık dağılımı",
    ["model_version", "role"],
    buckets=tuple(i / 20 for i in range(1, 21)),
    registry=REGISTRY,
)
DLQ_SIZE = Gauge("fraud_dlq_size", "Dead-letter kuyruğundaki mesaj sayısı", registry=REGISTRY)
DRIFT_PSI = Gauge("fraud_drift_psi", "Feature/skor PSI değeri", ["feature"], registry=REGISTRY)

# --- LLM ---------------------------------------------------------------------
LLM_CALLS = Counter("fraud_llm_calls_total", "LLM çağrıları", ["task", "mode"], registry=REGISTRY)
LLM_FAILURES = Counter(
    "fraud_llm_failures_total", "LLM hataları (fallback'e düşen)", ["kind"], registry=REGISTRY
)
LLM_LATENCY = Histogram(
    "fraud_llm_latency_seconds",
    "LLM çağrı gecikmesi",
    ["mode"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32),
    registry=REGISTRY,
)
LLM_USAGE = Counter("fraud_llm_tokens_total", "LLM token kullanımı", ["type"], registry=REGISTRY)

# --- persistence / bus -------------------------------------------------------
DB_WRITE_ERRORS = Counter(
    "fraud_db_write_errors_total", "Başarısız toplu DB yazımları", registry=REGISTRY
)
DB_BATCH_SIZE = Histogram(
    "fraud_db_batch_size",
    "Toplu yazım başına kayıt sayısı",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
    registry=REGISTRY,
)
BUS_EVENTS = Counter(
    "fraud_bus_events_total", "Olay veriyolu mesajları", ["topic", "outcome"], registry=REGISTRY
)
BUS_BACKLOG = Gauge("fraud_bus_backlog", "Tüketilmeyi bekleyen mesajlar", registry=REGISTRY)
