# Plan v2: Denetim bulgularını kapatma ve gerçek açık veriyle doğrulama

Bu belge V0 fazının çıktısıdır. Bağımsız denetimin bulguları kodda yeniden
üretildi. Her bulgu önce `tests/test_audit_v2.py` içinde **başarısız bir testle**
(`xfail(strict=True)`) kayda geçirildi. Bulguyu kapatan commit testin işaretini
kaldırır. Böylece düzeltmeden önce yeşile dönen ya da düzeltmeden sonra geri
kırılan bir test paketi durdurur.

## 1. Yeniden üretilen bulgular (V0 kanıtı)

| # | Bulgu | Yeniden üretim | Kök neden |
|---|---|---|---|
| A1 | Copilot çıktısı kendi şemasını bozuyor | Demo popülasyonunda 5.096 TRY'lik transfer, ardından arka plan zenginleştirmesi `OutputRejected: String should have at least 5 characters` hatasını veriyor ve ŞİB kayboluyor | Yazma ertelemeli (write-behind) olduğu için `transactions` satırı henüz yok. `get_case` aracı `ts=""` döndürüyor, ŞİB şablonunda `ne_zaman = " – "` (3 karakter) oluyor. `_enrich_case` hatayı `except` ile yutuyor |
| A2 | STEP_UP yanlış pozitif döngüsü | Yeni telefondan yüksek tutar: STEP_UP. OTP sonucu bildirilemiyor, aynı cihaz her seferinde yeni sayılıyor | `ScoringEngine.commit` yalnız `ALLOW` kararında öğreniyor. OTP ya da analist geri bildirimi profile ulaşmıyor |
| A3 | Eğitim/canlı kayması | Backfill `label == 0` koşuluyla, canlı sistem `decision == ALLOW` koşuluyla öğreniyor | İki ayrı öğrenme kuralı var |
| A4 | Kural tabanı eşik altı riski HOLD'a zorluyor | 40 demo müşterisinden 25'inde günün ikinci transferi (800 → 5.096 TRY, yeni alıcı) risk 0,31–0,42 iken HOLD aldı, hesap INCELENIYOR'a geçti | `R_DRAIN_PATTERN` kuralının `action_hint: HOLD` tabanı riske bakmıyor |
| A5 | Kart testi ATO olarak tipleniyor | `card_testing` senaryosu ATO vakası açıyor | `KART_TESTI`, `ATO`'dan düşük sıralı; aynı olayda ATO etiketli kurallar kazanıyor |
| A6 | Graf mağduru halkaya dahil ediyor; BLOCK sonrası `fraud_nodes` 0 | Fan-in kaynağı mağdurlar Louvain topluluğunda "üye" görünüyor | Yönsüz projeksiyon; `flag_fraud` yalnız analist onayında çağrılıyor |
| A7 | PSI açılışta alarm veriyor | Demo açılışında `device_age_d` 8,3, `payee_age_d` 6,8, skor 0,45 | Referans, farklı üretici sürümü ve farklı pencere dağılımından geliyor |
| B8 | Eşzamanlı velocity eksik sayılıyor | Aynı müşteriden 50 eşzamanlı işlem | Bus, `transaction_id` ile bölümlüyor; snapshot ve commit ayrı adımlar |
| B9 | Sınırsız Redis hash'leri | `fs:payee_first`, `fs:dev_first` TTL'siz | Genel (global) hash kullanılıyor |
| B10 | Redis tekrar teslimi sayaçları iki kez artırıyor | Aynı işlem iki kez commit edildiğinde pencere 2 olay içeriyor | Commit, `transaction_id`'ye göre idempotent değil; kilit yok |
| B11 | HTTP p99 265 ms | Ölçüm V6'da | Senkron vaka/audit yazımı ve SSE yayını karar yolunda |
| B12 | Sıraya bağlı test | `pytest -p randomly` ile `test_simulator.py::test_run_publishes_traffic_and_scenarios` kırılıyor | Paylaşılan global durum |
| B13 | Tatil takvimi yalnız 2026 | 2027 Ramazan ve 2028 Kurban bayramları sayılmıyor | Sabit liste |
| B14 | `high_risk_countries` FATF listesiyle uyuşmuyor | UA ve AE listede | El ile yazılmış dize |
| B15 | Yaptırım taraması lineer, ikincil anahtar yok | Tek kelimelik takma ad yakalanmıyor, doğum tarihi yok sayılıyor | Lineer tarama; eşleşme güveni hesaplanmıyor |
| C16 | Demo kullanıcılar her ortamda ekleniyor | `create_app` her zaman `with_demo_users()` çağırıyor | Ortam kontrolü yok |
| C17 | SSE JWT'si sorgu dizesinde | `?access_token=` | `stream_principal` |
| C18 | `/metrics` herkese açık | Kimlik doğrulama yok | |
| C19 | Ingest limiti 200.000/dk, HMAC'ta nonce yok | Ayar değeri ve imza fonksiyonu | |
| C20 | PII redaction ASCII'ye katlanmış isimleri kaçırıyor | "Ayse Yilmaz" maskelenmiyor | Sözlük tabanlı, katlama tek yönlü |
| C21 | İç LLM sunucusunun adresi kodda ve dokümanda | `git grep` 4 dosyada eşleşme buluyor | Varsayılan olarak yazılmış |
| D22–25 | Frontend testsiz, erişilebilirlik ve ekran görüntüsü eksik | `frontend/` altında test yok, `docs/img` boş | |
| E26–31 | Dokümanlarda eşitlik tablosu, etiketsiz performans iddiası, statik rozetler | README ve dokümanlar | |

## 2. Faz sırası

| Faz | İçerik | Çıkış kriteri |
|---|---|---|
| V0 | Bu belge ve `tests/test_audit_v2.py` | Kırmızı testler eklendi |
| V1 | A1–A3 | Parite testi yeşil; her vaka tipinde otomatik ŞİB/özet üretiliyor |
| V2 | Veri indirme, fixture'lar, `docs/DATA.md` | Checksum'lar kaydedildi |
| V3 | Üreticiyi parmak izinden arındırma, yeniden eğitim | Özellik baskınlığı ≤ %35, sızıntı dedektörü şans seviyesinde |
| V4 | PaySim replay, ablasyon, Elliptic, ULB | `docs/VALIDATION_REPORT.md` |
| V5 | A4–A7, B8–B10, B12–B15 | Rastgele sırada 3 tur yeşil |
| V6 | B11 performans | HTTP p99 < 50 ms (tek istemci) |
| V7 | C16–C21 güvenlik | `git grep` temiz, SSE bileti çalışıyor |
| V8 | D22–D25 arayüz | Vitest + Playwright, `docs/img` dolu |
| V9 | E26–E31 dokümantasyon | Eşitlik tablosu yok, "Sınırlamalar" var |
| V10 | Bağımsız denetim döngüsü | ≥ 9/10 ya da gerekçeli kalan bulgular |

## 3. Tasarım kararları

- **Tek öğrenme kuralı (A2/A3).** `app/features/learning.py::should_learn(decision, feedback)`:
  - Doğrulanmış fraud geri bildirimi → hiçbir zaman öğrenilmez.
  - Başarılı step-up (OTP) veya analistin "temiz" etiketi → öğrenilir. Cihaz ve alıcı "doğrulanmış" sayılır.
  - Geri bildirim yoksa → yalnızca `ALLOW` öğrenilir.

  Backfill aynı kural motoru ve politika katmanıyla karar üretir. Step-up sonucunu
  etikete göre simüle eder: meşru müşteri OTP'yi geçer, fraud geçemez. Canlı
  sistem aynı fonksiyonu `step-up-result` uç noktası ve analist etiketiyle çağırır.
- **Kart testi ayrı tip (A5).** Vaka ve alert tipi `CARD_TESTING` oluyor. Eski
  `KART_TESTI` kayıtları okunurken bu tipe eşlenir.
- **Graf yönü (A6).** Halka çıkarımında yalnızca para gönderen ve ağdan para
  almayan müşteriler (fan-in kaynağı) `affected` listesine yazılır, üye sayılmaz.
  BLOCK kararında karşı taraf düğümleri (alıcı hesap ve cihaz) `fraud` işaretlenir.
  Müşteri düğümü yalnızca analistin fraud etiketiyle işaretlenir. ATO'da müşteri
  mağdurdur; onu işaretlemek riski yanlış yöne yayar.
- **Kural tabanı (A4).** Bir kuralın HOLD/BLOCK tabanı artık `floor_min_risk`
  koşuluna bağlı (config). Risk bu değerin altındaysa taban bir kademe düşer
  (HOLD → STEP_UP). Tutar üst sınırı config'te durur. Yaptırım ve bilinmeyen
  müşteri override'ları değişmez.

## 4. Görev tanımından bilinçli sapmalar

Gerekçeleriyle birlikte, sapma ortaya çıktıkça bu bölüme eklenir.
