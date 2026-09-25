# Performans

Hedef (Anil3.md §2, P0.5): **senkron skor yolu p99 < 50 ms** (LLM hariç). Ölçüm aracı: `scripts/load_test.py`.

- **engine** — süreç içi `ScoringEngine.score` (feature store → kurallar → LightGBM → IForest/ECOD → graf / APP / river / konsorsiyum sinyalleri → politika), 200 işlemlik ısınmadan sonra. DoD #4 ölçütü budur.
- **http** — Docker compose stack'ine (PostgreSQL, Redis) eşzamanlı, kimlikli `POST /api/transactions`: doğrulama, olay veriyolu, skorlama, aksiyon, write-behind kalıcılık ve HTTP dahil uçtan uca süre. Simülatör ve senaryo trafiği aynı anda çalışırken alındı; tek API süreci (tek uvicorn worker).

Gözlemler: TreeSHAP (~2 ms) yalnız ALLOW olmayan veya risk ≥ 0,2 kararlarda hesaplanır; LightGBM tek satır tahmini tek iş parçacığında çalışır (OpenMP başlatma maliyeti kuyruk gecikmesini bozuyordu). Birim testi `tests/test_scoring_engine.py::TestLatencyBudget` her CI koşusunda 1.000 işlemde p99 < 50 ms'yi doğrular. Yatay ölçek: API replikaları + Redis feature store + Redis Streams consumer group.

## Ölçümler

### Ölçüm — 2026-09-25 02:48 · Windows · Python 3.11.4 · 8 CPU

| Mod | İstek | Eşzamanlılık | TPS | p50 ms | p95 ms | p99 ms | max ms | Hata |
|---|---|---|---|---|---|---|---|---|
| engine | 5000 | 1 | 183.6 | 4.133 | 10.689 | 16.889 | 280.744 | 0 |

### Ölçüm — 2026-09-25 02:53 · Windows · Python 3.11.4 · 8 CPU

| Mod | İstek | Eşzamanlılık | TPS | p50 ms | p95 ms | p99 ms | max ms | Hata |
|---|---|---|---|---|---|---|---|---|
| http | 3000 | 32 | 11.3 | 899.219 | 14026.893 | 30020.181 | 34151.225 | 70 |

### Ölçüm — 2026-09-25 03:07 · Windows · Python 3.11.4 · 8 CPU

| Mod | İstek | Eşzamanlılık | TPS | p50 ms | p95 ms | p99 ms | max ms | Hata |
|---|---|---|---|---|---|---|---|---|
| http | 3000 | 32 | 50.2 | 523.881 | 1381.597 | 1906.345 | 2297.839 | 0 |

## Yorum

| Ölçüm | Sonuç | Değerlendirme |
|---|---|---|
| Senkron skor yolu (engine, 5.000 işlem) | **p50 4,1 ms · p95 10,7 ms · p99 16,9 ms**, tek çekirdekte ~184 işlem/sn | **Hedef karşılandı** (p99 < 50 ms) |
| Uçtan uca HTTP, ilk ölçüm | 11 TPS, p99 30 sn, 70 hata | Her istek tüm veriyolunun boşalmasını (`drain`) bekliyordu → düzeltildi: istek yalnız kendi kararını bekler (işlem başına Future) |
| Uçtan uca HTTP, düzeltme + `BUS_PARTITIONS=8` | **50 TPS, 0 hata**, p50 0,52 sn, p99 1,9 sn (32 eşzamanlı istemci + simülatör + senaryo trafiği) | 1.000 TPS hedefine **ulaşılmadı** |

**Darboğaz:** tek uvicorn süreci (GIL) HTTP ayrıştırma, JWT, pydantic doğrulama, skor (~5 ms), hesap/vaka yazımları, SSE yayını ve LLM zenginleştirmesini aynı olay döngüsünde yürütüyor; laptop'ta PostgreSQL/Redis/Grafana/simülatör de aynı CPU'yu paylaşıyor. **Ölçekleme yolu:** API replikaları (`docker compose up --scale api=N` + yük dengeleyici) — feature store ve olay omurgası zaten Redis üzerinde paylaşılır; ingest'in Redis Streams'e yazıp karar olayını egress'ten beklemesi; vaka/audit yazımlarının tamamen write-behind yapılması. Skor çekirdeği (~184 işlem/sn/çekirdek) 8 çekirdekte ~1.400 işlem/sn teorik tavan verir.
