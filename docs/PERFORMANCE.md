# Performans

Bu belge denetim bulgusu **B11**'in (Anil3v2 §2.B.11) kapanış raporudur. Ölçüm aracı `scripts/load_test.py`, profil aracı `pyinstrument` 5.1. Buradaki bütün sayılar bu dizüstü bilgisayarda gerçekten ölçüldü. Hedefe ulaşmayan sonuçlar da yazıldı; hiçbir sayı yuvarlanarak iyileştirilmedi.

Üç ayrı ölçüm vardır ve bunlar birbirinin yerine kullanılmamalıdır:

- **Motor**, süreç içi `ScoringEngine.score` çağrısıdır. Feature store, kurallar, LightGBM, IForest/ECOD, graf, APP, river ve politika katmanını kapsar; HTTP, veriyolu ve veritabanı yoktur. README'deki "p99 < 50 ms" rozeti yalnız bu ölçümü anlatır.
- **HTTP tek istemci**, kimlikli `POST /api/transactions` isteğinin sıralı gönderildiği durumdur; her istek bir öncekinin yanıtını bekler. Doğrulama, idempotency, skor, karar ve HTTP yığını dahil, istemcinin gördüğü uçtan uca süreyi verir.
- **HTTP eşzamanlı**, açık döngülü yük üretimidir. İstekler sabit hızla zamanlanır ve yavaş yanıtlar yeni isteklerin gönderilmesini geciktirmez; böylece *coordinated omission* hatasına düşülmez. "Planlanan p99" sütunu, isteğin planlandığı andan yanıt alınana kadar geçen süredir. Sistem hedef hıza yetişemiyorsa bu sütun büyür.

## Ortam

| | |
|---|---|
| Donanım | Intel i5-11300H: 4 çekirdek, 8 iş parçacığı, 16 GB RAM. Dizüstü bilgisayar, prize takılı |
| İşletim sistemi | Windows 11 Home, Python 3.11.4 |
| Docker | Docker Desktop 29.2.1, WSL2 VM'i: 8 vCPU, 7,6 GB |
| Paylaşım | Ölçümler sırasında aynı Docker daemon'unda başka ekiplerin iki compose yığını daha çalışıyordu. Yük istemcisi de sunucuyla aynı makinede koştu |
| Saat | Windows'ta `loop.time()` 15,6 ms adımlarla ilerlediği için bütün süreler `time.perf_counter()` ile ölçüldü |

## Profil bulguları

### Önce (tek süreç, SQLite, demo trafiği)

1.000 sıralı isteğin aşama süreleri:

| Aşama | Ortalama | p50 | p99 |
|---|---|---|---|
| `Pipeline.ingest` (HTTP handler içi toplam) | 39 ms | 28 ms | 218 ms |
| `stored_result`: her yeni işlem için DB'de "bu id daha önce görüldü mü?" sorgusu | 26,7 ms | 17,5 ms | 141 ms |
| `on_decision` → vaka/alarm yazımı (senkron) | 8,3 ms | | 98,7 ms |
| Kalıcılık yazarı toplu yazımı (aynı olay döngüsünde) | 60 ms | | 235 ms |
| `ScoringEngine.score` | 2,9 ms | | 5,3 ms |

Bu tablodan çıkan sonuç şudur: skor, istek süresinin yaklaşık %7'sini oluşturuyordu. Sürenin büyük kısmı idempotency için yapılan DB sorgusundan ve karar yolunda senkron çalışan vaka, audit ve graf yazımlarından geliyordu. Bunlara ek olarak copilot zenginleştirmesi (ortalama 1,25 s, p99 6,6 s) sınırsız sayıda eşzamanlı çalışıyordu ve olay döngüsünü tıkıyordu. SlowAPI ve ara katmanlar 65 saniyelik profilin yaklaşık 3,7 saniyesini, loglama yaklaşık 0,7 saniyesini alıyordu.

### Sonra (aynı ölçüm, değişikliklerden sonra)

83 saniyelik profilde 75,7 saniye CPU süresi ölçüldü. Bunun dağılımı:

| Kalem | CPU süresi | Açıklama |
|---|---|---|
| `ScoringEngine.score` | 29,2 s | LightGBM tahmini ve katkıları yaklaşık 8,8 s, IsolationForest/ECOD 6,6 s, öznitelik çıkarımı 3,4 s, graf 2,4 s, yaptırım taraması 1,9 s |
| SlowAPI, ara katmanlar ve yanıt yazımı | 13,9 s | Oran sınırlayıcı ve Starlette `BaseHTTPMiddleware` akışı |
| Loglama | ~4–5 s | ALLOW dışındaki her karar için yazılan `Action` WARNING satırı ile her HTTP isteği için yazılan erişim satırı |
| Veritabanı ve idempotency | profilde görünmüyor | Yazımlar arka plan kuyruğunda; yeni id'ler için DB sorgusu yapılmıyor |

Artık sistem **CPU'ya bağlı**: süreyi skor motoru ve HTTP yığını belirliyor, G/Ç beklemesi yok. Bundan sonraki kazanım ancak daha fazla çekirdekle ya da skor motorunu hızlandırarak elde edilebilir. Örneğin TreeSHAP yalnız eşik üstü kararlarda çalıştırılabilir, IForest ağaç sayısı düşürülebilir.

## Yapılan değişiklikler

1. **Senkron yolda yalnız skor, karar ve idempotency kaldı** (`app/pipeline.py`, `app/bus/writebehind.py`). Kararın yan etkileri, yani vaka/alarm açma, canlı akış (SSE) ve egress, sınırlı boyutlu bir write-behind kuyruğunda (`decision-effects`) sırayla yürütülür. Kuyruk dolarsa üretici bekler; iş düşürülmez. Kuyruk derinliği ve hata sayısı `WRITE_BEHIND_BACKLOG` ve `WRITE_BEHIND_ERRORS` metrikleriyle izlenir. Audit zinciri tek yazıcıyla ilerler: süreç içinde tek kuyruk kullanılır, süreçler arasında Postgres `pg_advisory_xact_lock` sıralamayı sağlar. Testler arka plan işini `wait_background()`, `drain()` veya `settle()` ile bekler; hiçbir test gevşetilmedi.
2. **Idempotency DB'ye gitmeden cevaplanıyor** (`app/idempotency.py`). Görülen id'ler ölçeklenebilir bir Bloom filtresinde tutulur. Yeni bir id kesin olarak "görülmedi" cevabı alır ve DB sorgusu yapılmaz. Filtre "belki görüldü" derse eski yol olan DB kontrolü devreye girer. Redis varsa her id `SET NX EX` ile sahiplenilir ve karar özeti de Redis'e yazılır. Böylece aynı `transaction_id` farklı bir worker'a gelse bile aynı kararı döner; eşzamanlı ikinci istek ilk kararın yazılmasını bekler.
3. **Copilot zenginleştirmesi sınırlandı.** En fazla `ENRICH_CONCURRENCY=2` zenginleştirme aynı anda çalışır ve karar yolunu beklemez.
4. **Çoklu worker desteği** (`WEB_CONCURRENCY`). SSE biletleri Redis'te `SET EX` / `GETDEL` ile tutulur, oran sınırı `RATE_LIMIT_STORAGE_URI` ile Redis'e taşındı. Isınma (geçmiş trafiğin yeniden oynatılması) Redis'te `SET NX` ile seçilen tek bir lider worker tarafından yapılır. Redis Streams tüketicisi `pipeline` consumer group'unu kullanır, bu yüzden her mesaj bir kez işlenir. `docker-compose.yml` bu ayarları geçirir.
5. **Log seviyeleri düşürüldü.** Ajanların işlem başına yazdığı INFO satırları DEBUG seviyesine indirildi.
6. **`scripts/load_test.py` genişletildi.** Tek istemci modu (`--mode http-seq`), açık döngülü eşzamanlı mod, karar dağılımı raporu, büyük popülasyon desteği (`--population`), gönderim anında damgalanan `ts` alanı ve konteyner saatine uyum için `--utc` eklendi.

### Süreç başına kalan durum (çoklu worker'da paylaşılmayanlar)

| Durum | Etkisi |
|---|---|
| `AccountService._status` önbelleği | Bir worker'da bloke edilen hesap, diğer worker'da DB'den yeniden okunana kadar önbellekteki eski durumuyla görünebilir. Kalıcı kayıt DB'dedir |
| `EntityGraph` (katır/halka grafiği) | Her worker kendi gördüğü işlemlerle graf kurar. Graf sinyali worker sayısı arttıkça zayıflar; paylaşımlı graf deposu kapsam dışı bırakıldı |
| `results` LRU'su ve `recent_live` | `GET /api/transactions/{id}` ve canlı akış geçmişi yalnız işlemi işleyen worker'da bulunur. Kalıcı sonuç DB'den okunur |
| SSE abone kuyrukları | Bir SSE bağlantısı yalnız bağlı olduğu worker'ın kararlarını görür. Biletler paylaşımlıdır, akışlar paylaşımlı değildir |
| Bloom filtresi | Worker'a özeldir. Worker'lar arası tekrarları Redis sahiplenmesi yakalar |
| Oran sınırlayıcı (`memory://`) | Varsayılan dev ayarında worker başına sayar. Compose Redis kullanır |
| Feature store | Redis modunda paylaşımlıdır, bellek modunda worker'a özeldir |

## Ölçümler

### Motor

| Ölçüm | İşlem | TPS | p50 ms | p95 ms | p99 ms | max ms |
|---|---|---|---|---|---|---|
| Önce (2026-09-25 02:48) | 5.000 | 183,6 | 4,13 | 10,69 | 16,89 | 280,7 |
| Sonra | 4.821 | 314,4 | 2,68 | 5,02 | **7,07** | 99,6 |

Motor kodu bu çalışmada değişmedi. Aradaki fark büyük ölçüde ölçüm koşullarından kaynaklanıyor: ilk ölçüm sırasında makinede Docker yığını da çalışıyordu. Hedef olan p99 < 50 ms her iki ölçümde de karşılandı.

### HTTP tek istemci (dev profili: tek uvicorn süreci, SQLite, `BUS_PARTITIONS=0`)

| Ölçüm | İstek | TPS | p50 ms | p95 ms | p99 ms | max ms |
|---|---|---|---|---|---|---|
| Önce (denetim) | | | | | 265 | |
| Önce (bu çalışmanın temel ölçümü) | 1.000 | 8,5 | 87,5 | 303 | 523 | 2.436 |
| Sonra, 1. koşu | 1.000 | 80,4 | 11,35 | 20,31 | 34,65 | 63,6 |
| Sonra, 2. koşu | 1.000 | 91,1 | 9,50 | 17,25 | **31,48** | 868,9 |

**Hedef (p99 < 50 ms) karşılandı.** p99 yaklaşık 15 kat düştü. 2. koşudaki 869 ms'lik tek aykırı değer, SQLite yazıcısının toplu commit'i ile çakışan bir istektir.

Docker compose yığınında (Postgres, Redis, 4 worker) tek istemci sonuçları şöyledir:

| Nereden | İstek | TPS | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| Windows host → yayınlanan port | 1.000 | 26,8 | 31,5 | 83,2 | 146,0 |
| Konteyner içinden (`docker exec`) | 500 | | 14,7 | 31,8 | **49,1** |

Sunucunun ölçtüğü `latency_ms` (skor ve karar) 4–9 ms arasındadır. Host'tan ölçülen sürenin yaklaşık 15 ms'si (p50) Docker Desktop'ın Windows port yönlendirmesinden (WSL2 + vpnkit) gelir; bu kısım uygulamanın dışındadır.

### HTTP eşzamanlı

Dev profili: tek süreç, SQLite, 32 istemci, açık döngü, 150 müşterilik demo trafiği.

| Hedef TPS | Gerçekleşen TPS | p50 ms | p95 ms | p99 ms | Planlanan p99 ms | Hata |
|---|---|---|---|---|---|---|
| Önce: 50 (denetim, kapalı döngü) | 50,2 | 524 | 1.382 | 1.906 | | 0 |
| Önce: 50 (bu çalışmanın temel ölçümü) | 4,5 | 3.700 | | 30.000 | | 90 |
| Sonra: 50 | 50,0 | 20,6 | 145,9 | 472,0 | 475,7 | 0 |
| Sonra: 100 | 98,9 | 264,0 | 578,5 | 861,5 | 2.416 | 0 |
| Sonra: 200 | 136,4 | 225,9 | 393,0 | 846,1 | 15.401 | 1.289 |

Tek süreç yaklaşık 100–136 TPS'de doyuyor. Bunun sebebi GIL: tek çekirdekte işlem başına yaklaşık 7–10 ms CPU harcanıyor.

Docker compose: Postgres, Redis, `WEB_CONCURRENCY=4`, `BUS_PARTITIONS=8`, 64 istemci, host'tan yük.

| Hedef TPS | Gerçekleşen TPS | p50 ms | p99 ms | Hata |
|---|---|---|---|---|
| 100 | 84,7 | 127 | 3.717 | 1 |
| 200 | 83,8 | 376 | 5.092 | 4 |
| 300 | 129,1 | 352 | 2.091 | 0 |

Bu koşularda API konteyneri yaklaşık %400 CPU'ya (4 çekirdek), Postgres %160 CPU'ya çıktı.

**Hedef (≥ 500 TPS, p99 < 150 ms) karşılanmadı.** Ölçülen en yüksek verim, p99 2,1 s ile 129 TPS'dir. Sebepleri şunlardır:

- **CPU tavanı.** İşlem başına yaklaşık 11 ms CPU harcanıyor: skor, HTTP yığını, Postgres yazımı ve JSON işleme. Makinenin 4 fiziksel çekirdeği teorik olarak en fazla yaklaşık 360 TPS'e izin verir. Postgres, Redis, yük istemcisi ve Docker VM'i de aynı çekirdekleri paylaşıyor. Bu yüzden 500 TPS bu donanımda ulaşılabilir değildir.
- **Trafik gerçekçiliği.** 150 müşterilik demo popülasyonu yüksek hızda tekrar tekrar oynatıldığında sistem bunu hız dolandırıcılığı olarak görüyor ve hesapları bloke ediyor. Bu yüzden ölçümlerdeki karar dağılımı gerçek trafikten çok farklıydı: ALLOW %0, BLOCK %48, HOLD %44, STEP_UP %8. BLOCK ve HOLD kararları vaka açtırdığı için yazma yükü gerçekçi trafiğe göre çok daha ağırdı. Sentetik kaynakla dağılım ALLOW %42, BLOCK %36, HOLD %6, STEP_UP %16 oldu. Daha adil bir ölçüm için 5.000 müşterilik bir popülasyon hazırlandı (`scripts/build_demo_population.py --customers 5000 --days 2`). Ancak bu popülasyonla ölçüme geçildiği sırada 4 worker'dan bazıları yeniden başladı ("Child process died") ve ardından Docker Desktop tamamen yanıt vermez hale geldi. Aynı daemon'u başka ekiplerin yığınları da kullandığı için Docker yeniden başlatılmadı. Bu nedenle 5.000 müşterilik ölçüm tamamlanamadı.
- **Windows port yönlendirmesi.** Her istek p50'de yaklaşık 15 ms ek gecikme alıyor.

### Tekrar üretme

```bash
# motor
python scripts/load_test.py --mode engine --count 5000
# tek istemci (dev sunucusu çalışırken)
python scripts/load_test.py --mode http-seq --count 1000 --url http://127.0.0.1:<port>
# eşzamanlı, açık döngü, büyük popülasyon, konteyner saati UTC
python scripts/load_test.py --mode http --tps 200 --seconds 30 --concurrency 64 \
  --population <dizin> --utc --url http://127.0.0.1:<port>
```

`--report <dosya>` sonucu Markdown tablosu ve karar dağılımıyla birlikte dosyaya ekler; `--source synthetic` sentetik trafik üretir.
