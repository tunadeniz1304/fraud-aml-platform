# Mimari

## Olay akışı

1. **Ingress** — `POST /api/transactions` (JWT / `X-API-Key` / HMAC imza, rate limit) veya Redis Streams `fraud:transaction.created` (consumer group, ack, XAUTOCLAIM, deneme sayacı, **DLQ**, `transaction_id` idempotency). Pydantic `TransactionIn` sıkı doğrulama; bozuk mesaj `transaction.rejected` / DLQ.
2. **TransactionMonitor** — temel kontroller (tutar, para birimi, zaman damgası, cihaz); iyi biçimli olay `transaction.monitored`.
3. **ContextAnalyst → ScoringEngine** (senkron, LLM'siz): hesap BLOKE ise skorlamadan BLOCK; bilinmeyen müşteri HOLD; aksi halde feature store (nokta-zamanlı pencereler) → harici sinyaller (burst, varlık grafı, APP/CoP, river HST, konsorsiyum) → kural DSL → LightGBM olasılığı → anomali (IForest + ECOD) → yaptırım taraması (tam + bulanık) → **PolicyEngine** (stacker + sinyaller → risk → aksiyon + override'lar) → reason code'lar (TreeSHAP yalnız açıklama gerektiğinde).
4. **ActionAgent** — hesap durum makinesi (sistem yalnız yükseltir: AKTIF → INCELENIYOR → BLOKE), write-behind toplu yazıcı ile `transactions` + `decisions` (bileşenler, reason code, kural/model versiyonu, gecikme, SHAP) + **hash-zincirli audit**.
5. **Karar olayı** — `decision.made`: vaka yönetimi (alert → vaka), SSE canlı akış, Redis egress (worker).
6. **Async katman** — copilot zenginleştirme (APP metin sınıflandırması, AML/mule/yaptırım vakalarına otomatik ŞİB taslağı), periyodik halka tespiti; worker: DB'den halka tespiti, SLA/MASAK izleme, audit doğrulama, retrain önerisi.

## Skor katmanları

| Katman | Modül | Not |
|---|---|---|
| Feature store | `app/features/` | 46 feature, online = offline (eğitimde aynı fonksiyonlar), Redis veya bellek |
| Kural DSL | `app/scoring/rules.py`, `rules/core.yaml` | AST whitelist, `eval` yok; noisy-OR; action floor; DB'de versiyonlu |
| ML | `app/ml/` | LightGBM + TreeSHAP; IForest (dizi tabanlı, sklearn ile birebir); ECOD; lojistik stacker |
| Graf | `app/graph/` | pass-through, döngü, fraud yakınlığı, union-find halka, Louvain + PageRank |
| APP / CoP | `app/app_scam/` | alıcı doğrulama, sosyal mühendislik sinyalleri, dinamik uyarı |
| Online profil | `app/profile/online.py` | river Half-Space Trees, yüzdelik kalibrasyon |
| Politika | `app/scoring/policy.py` | eşikler (runtime ayarlanabilir), override'lar, tipoloji tavanı |

## Veri modeli

```mermaid
erDiagram
  customers ||--|| accounts : sahip
  accounts ||--o{ account_status_history : gecmis
  customers ||--o{ transactions : yapar
  transactions ||--|| decisions : skorlanir
  transactions ||--o{ alerts : uretir
  cases ||--o{ alerts : gruplar
  cases ||--o{ case_events : zaman_cizelgesi
  transactions ||--o{ labels : etiketlenir
  rules ||--o{ rule_versions : versiyonlanir
  models ||--o{ decisions : skorlar
  approvals }o--|| cases : "SIB / UNBLOCK / MODEL_PROMOTE"
  graph_rings }o--o{ customers : uyeler
  audit_log }o--o{ transactions : "SHA-256 zincir"
```

Para `NUMERIC(18,2)` + para birimi + TRY karşılığı. Şema Alembic ile yönetilir (`0001`, `0002`); testler migration ↔ model eşitliğini doğrular.

## Güvenlik

JWT (HS256) + hiyerarşik RBAC (`analist < kidemli_analist < admin`), servis anahtarı / HMAC imzası (replay penceresi), slowapi rate limit, CORS whitelist, katı CSP (`unsafe-inline` yok), request-id korelasyonu, sanitize edilmiş 500, maker-checker (bloke kaldırma, ŞİB, model terfisi).
