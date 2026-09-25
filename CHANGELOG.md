# Değişiklik günlüğü

Biçim: [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/) · sürümleme: [SemVer](https://semver.org/lang/tr/).

## [2.0.0] — 2026-09-25

### Eklendi
- **LLM sözleşmesi:** OpenAI-uyumlu async istemci (DeepSeek V4 Flash @ Evren), CANLI / DEMO / fallback modları, pydantic şema doğrulaması + onarım denemesi, KVKK pseudonimleştirme, `GET /api/llm/status`, `scripts/llm_smoke.py`.
- **Olay omurgası:** Redis Streams (consumer group, ack, retry, DLQ, idempotency) + bellek içi veriyolu; backpressure.
- **Kalıcılık:** PostgreSQL + Alembic, NUMERIC para + TRY normalizasyonu, hash-zincirli audit (`/api/audit/verify`).
- **Streaming feature store:** 46 feature, EWMA adaptif profil, Redis/bellek arka uçları.
- **Hibrit skorlama:** güvenli kural DSL (YAML/DB, versiyonlu, backtest), LightGBM + TreeSHAP reason code, IsolationForest + ECOD, lojistik stacker, politika katmanı (ALLOW/STEP_UP/HOLD/BLOCK), bulanık yaptırım taraması.
- **Sentetik veri + eğitim:** seed'li Türk bankacılığı verisi (ATO, APP, mule, kart testi, smurfing, yaptırım), zaman bazlı eğitim, model kartı, `fraud_gbm_v1` (champion) ve `fraud_gbm_v2` (challenger).
- **Vaka yönetimi:** alert→vaka gruplama, iç SLA + MASAK 10 iş günü, atama/not/kanıt, FRAUD/TEMIZ kararı → etiket, maker-checker (bloke kaldırma, ŞİB, model terfisi).
- **Gözlemlenebilirlik:** Prometheus metrikleri, OpenTelemetry shim, Grafana/Prometheus profili ve hazır pano.
- **Varlık grafı / APP / online profil:** mule pass-through, katmanlama döngüsü, Louvain halkaları; Confirmation of Payee + dinamik uyarılar; river Half-Space Trees; tipoloji tavanı.
- **LLM copilot:** tool-use döngüsü (ReAct yedeği), atıf doğrulamalı özet / karar önerisi / ŞİB taslağı (PDF/JSON), SSE analist sohbeti, otomatik ŞİB taslağı.
- **Model yönetişimi:** gölge skorlama, karşılaştırma, PSI drift, aktif öğrenme, retrain önerisi.
- **Analist konsolu:** React + Vite + TS + Tailwind SPA (canlı akış, vaka kuyruğu, vaka detayı + SHAP + Cytoscape, kural stüdyosu, model izleme, senaryo tetikleyici); Docker multi-stage.
- **Konsorsiyum:** tuzlu SHA-256 paylaşımlı kara liste + FedAvg demosu; **yük testi** betiği ve performans raporu.
- CI (ruff, mypy, pytest+cov, frontend build, docker build), ADR'ler, mimari/uyum/demo dokümanları.

### Düzeltildi
- v1'deki 15 bilinen hata (Docker'da pipeline kurulmaması, simülatör, dashboard interpolasyonu, monoton olmayan hesap durumu, BLOKE hesabın skorlanması, ölü sinyaller, bozuk işlemin skorlanması, bilinmeyen müşteri, senkron LLM, sabit-zamanlı olmayan token karşılaştırması, sınırsız tamponlar, para birimi, test verisinin ezilmesi, sahte RAG, O(n²) rapor / SQLite kilidi / CSP) — her biri için regresyon testi.
- Yeniden başlatma sonrası kalıcı bir işlem tekrar gönderildiğinde 500 yerine saklı kararı döndürme.

## [1.0.0]
- İlk sürüm: in-memory, kural ağırlıklı fraud analisti ajanı.
