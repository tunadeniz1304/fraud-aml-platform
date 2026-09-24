# Dönüşüm Planı — Anil3 v2 (Fraud & AML Platformu)

Bu belge F0 keşif fazının çıktısıdır: mevcut kodun doğrulanmış durumu, hedef
mimari, faz planı ve görev tanımından **bilinçli sapmalar**.

## 1. Doğrulanan başlangıç durumu

- Python 3.11, FastAPI, pydantic v2, ChromaDB, SQLite; 52 test yeşil.
- Akış: `EventBus` → `TransactionMonitor` → `ContextAnalyst` → `ActionAgent`.
- Görev tanımındaki 15 hatanın tamamı kodda doğrulandı (örn. `server.py`
  `build_pipeline()` yalnızca `__main__` altında; `frontend.py` içinde
  `" + API + r"` literal olarak JS'e gidiyor; `ActionAgent._warn` koşulsuz
  `INCELENIYOR` yazıyor; LLM çağrısı async handler içinde senkron).
- Başlangıçta commit edilmemiş kullanıcı işi (yaptırım taraması entegrasyonu)
  testleri geçtiği için F0'da ayrı commit olarak kaydedildi.

## 2. Hedef mimari (özet)

```
app/
  config.py            genel ayarlar (eşik/ağırlık/DB/Redis/auth) — sihirli sayı yok
  llm/                 LLM sözleşmesi: config, istemci, demo, redaction, servis
  bus/                 EventBus arayüzü: InMemoryBus + RedisStreamsBus (DLQ, idempotency)
  db/                  SQLAlchemy 2.0 async modeller, repository, hash-zincirli audit
  core/                fx, yaptırım, eski yardımcılar
  features/            kayan pencere feature store (memory/redis) + FeatureDefinition registry
  scoring/             kural DSL, APP motoru, politika katmanı, reason code, durum makinesi
  ml/                  ModelRegistry, LightGBM + IsolationForest/ECOD, river HST, SHAP
  graph/               networkx entity graph, mule skoru, Louvain halka tespiti
  cases/               alert→vaka, SLA, maker-checker onayları, etiketler
  copilot/             tool-use araştırmacı ajan, ŞİB taslağı, analist sohbeti
  monitoring/          Prometheus metrikleri, PSI drift, tracing, JSON log
  consortium/          hash'li paylaşımlı deny-list (3 banka simülasyonu)
  api/                 FastAPI (lifespan, JWT/RBAC, rate limit, SSE) + legacy dashboard
frontend/              React + Vite + TS + Tailwind analist SPA (Docker'da build)
```

Senkron skor yolu: doğrulama → hesap durumu kontrolü → feature'lar → kurallar →
ML (+SHAP) → anomali → graf → APP → yaptırım → konsorsiyum → politika. LLM bu
yolun **tamamen dışındadır**; kalıcılık write-behind (toplu yazıcı) ile yapılır.

## 3. Faz planı ve kalite kapısı

| Faz | İçerik |
|---|---|
| F0 | pyproject (ruff/mypy/pytest-cov), Makefile, bu plan |
| F1 | LLM sözleşmesi + 15 hata düzeltmesi + güvenlik temeli (JWT/RBAC, HMAC, rate limit) |
| F2 | Postgres/Alembic + Redis Streams bus + feature store |
| F3 | Sentetik veri + eğitim + hibrit skor + politika + reason code |
| F4 | Vaka yönetimi + gözlemlenebilirlik |
| F5 | Adaptif profil, graf, APP, cihaz/biyometri |
| F6 | Copilot + ŞİB, champion/challenger, drift |
| F7 | Analist SPA, yük testi, konsorsiyum |
| F8 | Dokümantasyon + CI + cila |

Kapı: `ruff check .` · `ruff format --check .` · `mypy app` ·
`pytest --cov=app` (≥ %80) · `docker compose build`.

## 4. Bilinçli sapmalar ve kararlar

1. **Chroma senkron yoldan çıkarıldı.** MiniLM gömme hesaplaması işlem başına
   5–20 ms ekliyor ve p99 < 50 ms hedefini tehdit ediyor; ayrıca "semantik
   mesafe" sinyali gürültülüydü. Chroma artık copilot için **gerçek RAG**
   (etiketli geçmiş vakalar + müşterinin normal davranış özeti) olarak async
   kullanılıyor. Model indirilemezse deterministik hash gömmeye düşer.
2. **ML açıklaması sıcak yolda LightGBM yerleşik TreeSHAP'i (`pred_contrib`)
   ile hesaplanır**; bu `shap.TreeExplainer` ile matematiksel olarak aynıdır
   (testle doğrulanır) ama ~10× daha hızlıdır. `shap` eğitim/model kartında
   global önem için kullanılır.
3. **ECOD sıcak yolda önceden hesaplanmış ECDF'lerle** skorlanır (pyod ECOD
   her tahminde eğitim setini yeniden birleştirir → gecikme). pyod ECOD
   eğitimde referans olarak kullanılır; sıralama uyumu testle doğrulanır.
4. **Worker servisi batch işleri** çalıştırır (Louvain halka tespiti, drift
   anlık görüntüsü, etiket tabanlı yeniden eğitim kontrolü). Gerçek zamanlı
   durum (graf, profiller) tek tüketici grubunda API sürecinde tutulur;
   ölçekleme notu ADR-001'de.
5. **LLM kararı skora girmez** (§6). Hata #6'daki "LLM kararı skora girmiyor"
   maddesi, LLM'i senkron yoldan tamamen çıkarıp danışman rolüne almakla
   çözüldü; yalnızca async APP metin sınıflandırması vaka önceliğine sınırlı
   ağırlıkla yansır.
6. **GraphSAGE (P2, opsiyonel)** yalnızca `.[gnn]` extras kuruluysa aktif;
   varsayılan kurulumda devre dışı ve UI'da "opsiyonel" olarak etiketli.
7. **Federated öğrenme** `flwr` yoksa saf numpy FedAvg (lojistik regresyon)
   ile simüle edilir.
8. `ActionAgent`'ın ayrı kararları (`blocked/warned/passed`) geriye dönük
   uyumluluk için korunur; yeni karar kümesi `ALLOW/STEP_UP/HOLD/BLOCK`
   bunlara eşlenir (`BLOCK→BLOKE`, `HOLD/STEP_UP→INCELENIYOR` kaydı,
   `ALLOW→GECTI`).
