# Değişiklik günlüğü

Biçim: [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/) · sürümleme: [SemVer](https://semver.org/lang/tr/).

## [2.1.0] — 2026-09-25

Bağımsız denetim bulgularını kapatan sürüm. Bulgular önce `tests/test_audit_v2.py` içinde `xfail(strict=True)` testleriyle kayda geçirildi (`docs/PLAN_v2.md`). Bu bölüm performans sayısı içermez; ölçümler `docs/PERFORMANCE.md` içindedir.

### Doğrulama
- Halka açık veri indiricisi: PaySim (Zenodo), Elliptic (PyG aynası), ULB (OpenML 1597); kaldığı yerden devam eden indirme, sabitlenmiş checksum'lar (`scripts/fetch_public_fraud_data.py`, `docs/DATA.md`).
- Repoda internetsiz testler için atıflı PaySim ve Elliptic örnekleri (`tests/fixtures/`), birebir kopyalanmış satırlar.
- Gölge replay ve katman ablasyonu: PaySim, Elliptic, ULB ve sentetik veri (`scripts/validate_public_data.py`, `app/validation/`, `docs/VALIDATION_REPORT.md`).
- Sentetik üreticiden tipoloji parmak izleri kaldırıldı; sızıntı dedektörü testi eklendi. Eski 0,971 PR-AUC'nin bir sızıntıdan geldiği raporlandı.
- `fraud_gbm_v5` champion, `fraud_gbm_v6` challenger; seçim yalnız doğrulama dönemi metrikleriyle, test bir kez raporlanarak ve dört göz onayıyla yapıldı (`scripts/champion_selection.py`). v1–v4 arşivlendi.
- Eksik cihaz ve nakit kanal göstergeleri feature olarak eklendi (46 → 48 feature).
- 70/15/15 satır bölmesi, tabakalı bootstrap güven aralıkları, GBM–yığın farkları, Brier/ECE kalibrasyonu; bütçe kesiminde eşit skorlar paylaştırılıyor.
- Stacker katsayı tabanları yerine doğrulama ablasyonuyla seçilen negatif olmayan Platt tipi birleştirici.
- Replay'de kâhin (oracle) step-up geri bildirimi yerine kâhin olmayan OTP sonuç modeli; Elliptic graf feature'ları adım başına tümevarımsal.

### Düzeltmeler
- **A1** Copilot otomatik ŞİB taslağı şema reddinde artık kaybolmuyor.
- **A2** Başarılı step-up veya temiz etiketle doğrulanan cihaz/alıcı profile öğreniliyor (`POST /api/transactions/{id}/step-up-result`).
- **A3** Backfill ve canlı sistem tek öğrenme kuralını paylaşıyor (`app/features/learning.py::should_learn`).
- **A4** Düşük riskli kural HOLD tabanları STEP_UP'a yumuşatıldı.
- **A5** Kart testi `ATO` yerine `CARD_TESTING` olarak tipleniyor.
- **A6** Fan-in mağdurları halka üyesi sayılmıyor; BLOCK kararında karşı taraf düğümleri işaretleniyor.
- **A7** Demo popülasyonunda yanlış PSI alarmı giderildi (prod dışında popülasyon referansı).
- **B8, B10** Aynı müşterinin skorlaması sıralanıyor (müşteri başına kilit); Redis commit'i idempotent Lua betiği.
- **B9** Global alıcı/cihaz ilk görülme hash'leri, TTL'li varlık başına anahtarlara dönüştü (`feature_entity_ttl_days`).
- **B12** Simülatör trafik testi deterministik.
- **B13** Türk resmî tatilleri her yıl için hesaplanıyor (`holidays.Turkey`, arife politikası ayarlanabilir).
- **B14** Yüksek riskli ülkeler versiyonlu FATF listesinden (`data/jurisdictions/fatf_2026-06.json`).
- **B15** Yaptırım taraması blocking indeksi ve ikincil anahtarlarla (doğum yılı, uyruk) eşleşme güveni.
- **B11** HTTP gecikme çalışması ayrı yürütülüyor; sonuçlar `docs/PERFORMANCE.md`.

### Güvenlik
- **C16** Demo kullanıcıları yalnız `SEED_DEMO_USERS` açıkken (varsayılan prod dışı).
- **C17** SSE için tek kullanımlık kısa ömürlü bilet (`POST /api/stream/ticket`); sorgu dizesinde JWT kabul edilmiyor.
- **C18** `/metrics` `METRICS_TOKEN` istiyor; Prometheus aynı değeri `ops/prometheus/metrics_token` dosyasından okuyor.
- **C19** Ingest hız sınırı düşürüldü; HMAC imzasında tek kullanımlık `X-Nonce`.
- **C20** PII maskeleme ASCII'ye katlanmış, büyük harfli ve alıcı adlarını da kapsıyor.
- **C21** İç LLM sunucu adresi koddan ve dokümanlardan kaldırıldı.

### Bağımsız denetim turu 1 düzeltmeleri
- **Yetki ve oturum:** kısa ömürlü, iptal edilebilir oturumlar; step-up sonucu tek kullanımlık challenge'a bağlı; maker-checker yarış koşuluna karşı güvenli, türe göre onaylayıcı rolü, kendi talebini onaylama yok; kural ve eşik değişiklikleri onaydan geçiyor; FRAUD kapanışı ve yeniden açma kıdemli analist istiyor; audit ve DLQ yalnız kıdemli analiste, DLQ yükleri maskeli.
- **Sertleştirme:** prod'da demo sırlarıyla açılış reddediliyor; anonim Grafana kapalı; kural DSL'i yalnız sayısal aritmetik, sınırlı literal ve süre sınırlı backtest; copilot'ta güvenilmeyen vaka metni sınırlandırılıyor ve araçlar vakanın müşterisiyle sınırlı; PII maskeleme boşluklu TCKN, yabancı IBAN, kart ve telefon varyantlarını kapsıyor; HSTS.
- **Dayanıklılık:** ingress mesajları DB commit'inden sonra ack'leniyor; idempotency özet karşılaştırması ve 409 "işleniyor" yanıtı; yazıcı partileri yeniden deneniyor, bölünüyor, dead-letter'a düşüyor; hesap durumu compare-and-set ile yazılıyor ve worker'lar arası eşitleniyor; eşik, kural ve model nesilleri worker'lar arası paylaşılıyor; feature kilidi alınamazsa HOLD; vaka alımı outbox ile kalıcı.
- **Audit:** isteğe bağlı HMAC anahtarlı audit zinciri ve doğrulama hatası metriği.
- **Uyum:** süresi geçmiş MASAK vakaları ayrı sayılıyor (`fraud_cases_masak_overdue`, `MASAKSuresiGecti` alarmı).
- **LLM:** ağ geçidi markası dokümanlardan ve varsayılanlardan kaldırıldı; eski ortam değişkeni adı geriye uyumluluk için kabul ediliyor.

### Arayüz
- Doğrulama görünümü, sunucu taraflı sayfalı ve filtreli vaka kuyruğu, erişilebilirlik iyileştirmeleri, arayüz testleri.
- `docs/img/` altında ekran görüntüleri.

### Dokümantasyon
- README: ürün eşitliği tablosu ve statik kapsam/p99 rozetleri kaldırıldı; "İlham alınan desenler" ve "Sınırlamalar" bölümleri, CI durum rozeti, gecikme iddiaları "motor (süreç içi)" olarak etiketlendi.
- `docs/COMPLIANCE.md`: her yasal referans resmî URL ve uygulayan modülle eşlendi; doğrulanamayanlar işaretlendi.
- `docs/MODEL_CARD.md`, `docs/ARCHITECTURE.md` champion `fraud_gbm_v5` modeline ve v2 değişikliklerine göre güncellendi; `docs/DATA.md`, `docs/VALIDATION_REPORT.md`, `docs/PLAN_v2.md` eklendi.

## [2.0.0] — 2026-09-25

### Eklendi
- **LLM sözleşmesi:** OpenAI-uyumlu async istemci (DeepSeek V4 Flash, OpenAI-uyumlu uç nokta), CANLI / DEMO / fallback modları, pydantic şema doğrulaması + onarım denemesi, KVKK pseudonimleştirme, `GET /api/llm/status`, `scripts/llm_smoke.py`.
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
