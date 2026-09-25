# Final rapor — Anil3 v2.0.0 (Bitiş tanımı, Anil3.md §8)

Tarih: 2026-09-25 · Ortam: Windows 11, Python 3.11, Docker 29.2, 8 CPU.

> Bu rapor v2.0.0 anındaki durumu anlatır. Sonraki bağımsız denetimde bu tablodaki bazı kanıtlar geçersiz çıktı: sentetik verideki 0,971 PR-AUC bir etiket sızıntısından geliyordu, performans satırı yalnızca süreç içi motor ölçümüdür. Güncel durum ve halka açık veriyle doğrulama için v2 raporu `docs/FINAL_REPORT_v2.md` olacak (henüz yazılmadı); o zamana kadar `docs/VALIDATION_REPORT.md` ve `CHANGELOG.md` [2.1.0] bölümüne bakın.

| # | Kriter | Durum | Kanıt |
|---|---|---|---|
| 1 | `docker compose up --build` → servisler healthy, dashboard açılıyor, giriş, canlı akış | ✅ | `api (healthy)`, `postgres (healthy)`, `redis (healthy)`, `worker`, `simulator` (+ `prometheus`, `grafana` profil). `/` React konsolu (`id="root"`), `/legacy` klasik pano; `scripts/smoke_dashboard.py` → **DASHBOARD SMOKE OK** (sağlık, 401, giriş, durum, işlem listesi, anahtarsız LLM durumu, canlı ingest, geçersiz ingest 422, LLM açıklaması, SPA + legacy, CSP). Canlı akış: SSE `/api/live/stream`, TPS/p99 `/api/live/summary`. |
| 2 | `.env` yokken DEMO; varken CANLI; anahtar hiçbir yerde görünmüyor | ✅ | Test paketi `.env`'siz demo modunda koşar (copilot özet/öneri/ŞİB/sohbet dahil — `tests/test_copilot.py`). Canlı stack logu: `LLM: CANLI (<model> @ <yapılandırılmış sunucu>)`; `python scripts/llm_smoke.py` → `OK model=deepseek-v4-flash latency=2272ms`. API loglarında `sk-` sayısı 0; `/api/llm/status` yalnız `key_present`. Canlı model yavaşken çağrılar `llm_mode="fallback"` ile demo çıktısına düştü (sistem çökmedi). |
| 3 | Dört senaryo → beklenen aksiyonlar (entegrasyon testli) | ✅ | `tests/test_scenarios.py` + canlı HTTP: **ATO → BLOCK** (vaka `ATO`), **APP → HOLD** + dinamik uyarı ("Polis, savcılık… güvenli hesaba…"), müşteri onay akışı; **mule halkası → HOLD** + vaka `MULE` + halka ("RING-…: 7 hesap, 1 paylaşılan cihaz, toplam 355B TL"); **smurfing → HOLD** + `AML` vakası + otomatik **ŞİB taslağı** (`SIB_TASLAK`) → FRAUD kararı → maker-checker onayı → `SIB_GONDERILDI`. |
| 4 | Senkron skor p99 < 50 ms (motor, süreç içi) | ✅ | `scripts/load_test.py --mode engine --count 5000`: **p50 4,1 · p95 10,7 · p99 16,9 ms** (docs/PERFORMANCE.md); CI testi 1.000 işlemde p99 < 50 ms. Uçtan uca HTTP: 50 TPS, p99 1,9 sn (1.000 TPS hedefine ulaşılmadı — darboğaz analizi ve ölçekleme yolu raporda). |
| 5 | Model metrikleri + model kartı + champion/challenger ekranı | ✅ | `fraud_gbm_v1` (champion) test PR-AUC **0,971**, ROC-AUC 0,993, recall@%1 FPR 0,969; `fraud_gbm_v2` (challenger) PR-AUC 0,967. `docs/MODEL_CARD.md`, `models/*/model_card.md`. Konsol "Model izleme": çevrimdışı/çevrimiçi karşılaştırma, gölge skorlama, PSI drift, maker-checker terfi (`tests/test_copilot.py::test_promotion_is_maker_checker`). |
| 6 | §1'deki 15 hata düzeltildi ve testli | ✅ | `tests/test_regressions.py` (hata başına bir sınıf). |
| 7 | ruff, mypy, pytest --cov ≥ %80; test sayısı ≫ 52 | ✅ | `ruff check .` ✔ · `ruff format --check .` ✔ · `mypy app` ✔ (114 dosya) · **388 test**, kapsam **%94**. |
| 8 | README + docs + CI | ✅ | `README.md` (Mermaid mimari, kıyas tablosu, 30 sn'de çalıştır), `docs/ARCHITECTURE.md`, `docs/adr/` (6 ADR), `docs/MODEL_CARD.md`, `docs/COMPLIANCE.md`, `docs/PERFORMANCE.md`, `docs/DEMO_SCRIPT.md`, `.github/workflows/ci.yml`, `CHANGELOG.md`, `LICENSE` (MIT). |
| 8b | Git kuralları | ✅ | Küçük Conventional Commit'ler `origin/main`'e push'landı; secret taraması her commit'te 0; `.env`, `Anil3.md`, `handoff.md` repoda yok; tag `v2.0.0`. |

## Bilinen sınırlamalar
- Uçtan uca HTTP verimi tek API sürecinde ~50 TPS; 1.000 TPS için yatay ölçek gerekir (docs/PERFORMANCE.md).
- Modeller sentetik veriyle eğitildi; gerçek veride yeniden eğitim ve eşik kalibrasyonu şart.
- Federated öğrenme numpy FedAvg demosudur (`flwr` opsiyonel extra); GraphSAGE uygulanmadı (opsiyonel P2).
- Ekran görüntüleri v2.0.0'da yoktu; v2.1.0'da `docs/img/` altına eklendi.
