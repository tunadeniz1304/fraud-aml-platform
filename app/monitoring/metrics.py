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
AUDIT_VERIFY_FAILURES = Counter(
    "fraud_audit_chain_verify_failures_total",
    "Audit zinciri doğrulama hataları (alarm: > 0)",
    registry=REGISTRY,
)
# set by the worker's periodic ``audit_verify`` job; the worker serves its own
# /metrics (``WORKER_METRICS_PORT``), scraped as job ``anil3-worker``
AUDIT_VERIFY_LAST_OK = Gauge(
    "fraud_audit_chain_last_verify_ok",
    "Son periyodik audit zinciri doğrulaması başarılı mı (1/0)",
    registry=REGISTRY,
)
AUDIT_VERIFY_LAST_RUN = Gauge(
    "fraud_audit_chain_last_verify_timestamp_seconds",
    "Son periyodik audit zinciri doğrulamasının zamanı (unix s)",
    registry=REGISTRY,
)
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
# --- cases / compliance -------------------------------------------------------
CASES_OPENED = Counter(
    "fraud_cases_opened_total", "Açılan vaka sayısı", ["case_type"], registry=REGISTRY
)
CASE_DECISIONS = Counter(
    "fraud_case_decisions_total", "Analist vaka kararları", ["outcome"], registry=REGISTRY
)
CASES_OPEN = Gauge("fraud_cases_open", "Açık vaka sayısı", registry=REGISTRY)
SLA_BREACHED = Gauge(
    "fraud_cases_sla_breached", "İç SLA'sı (4 saat) aşılmış açık vaka", registry=REGISTRY
)
MASAK_DUE_SOON = Gauge(
    "fraud_cases_masak_due_soon", "MASAK süresine ≤2 iş günü kalan açık vaka", registry=REGISTRY
)
MASAK_OVERDUE = Gauge(
    "fraud_cases_masak_overdue", "MASAK bildirim süresi geçmiş açık vaka", registry=REGISTRY
)
SIB_SUBMITTED = Counter("fraud_sib_submitted_total", "Onaylanıp gönderilen ŞİB", registry=REGISTRY)
HTTP_REQUESTS = Counter(
    "fraud_http_requests_total", "HTTP istekleri", ["method", "route", "status"], registry=REGISTRY
)
HTTP_LATENCY = Histogram(
    "fraud_http_request_seconds",
    "HTTP istek süresi",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
MODEL_INFO = Gauge(
    "fraud_model_info", "Yüklü model versiyonları", ["role", "version"], registry=REGISTRY
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
STEP_UP_RESULTS = Counter(
    "fraud_step_up_results_total", "Step-up (OTP) sonuçları", ["outcome"], registry=REGISTRY
)
COPILOT_ERRORS = Counter(
    "fraud_copilot_errors_total",
    "Şemayı geçemeyen copilot çıktıları (deterministik minimal taslağa düşülen)",
    ["task"],
    registry=REGISTRY,
)

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
WRITER_DEAD_LETTERS = Counter(
    "fraud_writer_dead_letters_total",
    "Yazılamayıp DLQ'ya alınan (stored) ya da yalnız loglanan (log_only) kayıtlar",
    ["outcome"],
    registry=REGISTRY,
)
BUS_EVENTS = Counter(
    "fraud_bus_events_total", "Olay veriyolu mesajları", ["topic", "outcome"], registry=REGISTRY
)
BUS_BACKLOG = Gauge("fraud_bus_backlog", "Tüketilmeyi bekleyen mesajlar", registry=REGISTRY)
WRITE_BEHIND_BACKLOG = Gauge(
    "fraud_write_behind_backlog",
    "Karar sonrası yan etki kuyruğu (vaka, canlı akış, egress) bekleyen olaylar",
    registry=REGISTRY,
)
WRITE_BEHIND_ERRORS = Counter(
    "fraud_write_behind_errors_total",
    "Başarısız karar sonrası yan etkiler",
    ["handler"],
    registry=REGISTRY,
)
