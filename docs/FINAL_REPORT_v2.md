# Final rapor v2 (2.1.0)

Bu rapor iki denetim turunun sonucunu özetler. İlk olarak 2.0 sürümünün bağımsız denetiminde
bulunan 31 bulgunun (A1–E31) nasıl kapatıldığını anlatır. İkinci olarak 2.1 adayının bağımsız
denetimini (tur 1, 7/10) ve orada bulunan 37 bulgunun (H1–H7, M1–M17, L1–L13) kapatılmasını
anlatır. Ayrıca platformun halka açık gerçek fraud verisiyle nasıl doğrulandığını gösterir.
Ayrıntılı yöntem ve tablolar şu dokümanlardadır:
[`VALIDATION_REPORT.md`](VALIDATION_REPORT.md), [`PERFORMANCE.md`](PERFORMANCE.md),
[`DATA.md`](DATA.md), [`PLAN_v2.md`](PLAN_v2.md), [`MODEL_CARD.md`](MODEL_CARD.md).

## 1. Özet

| Konu | Sonuç |
|---|---|
| 2.0 denetimi | 31 bulgunun 30'u kapatıldı ve her biri için regresyon testi var. B11'in eşzamanlı hedefi (500 TPS, p99 < 150 ms) tek düğümde karşılanamadı; gerekçesi §4'te. |
| 2.1 denetimi, tur 1 | 7/10. 37 bulgunun hepsi için düzeltme commit'i yazıldı. Tur 2 (7,5/10) bunların 23'ünü kapalı, 14'ünü kısmi buldu ve 25 yeni bulgu ekledi. Tur 3 (8,0/10, son tur) izlenen 39 maddenin 26'sını kapalı, 11'ini kısmi, 2'sini açık buldu. Tur 3 sonrası düzeltmeler ve açık kalanların gerekçeleri §6'da. |
| Gerçek veri | PaySim (tam dosya, %10 alıcı örneği), Elliptic (tam graf) ve ULB (OpenML 1597) indirildi. Checksum'lar `scripts/public_data_checksums.json` içinde. Repoda internetsiz çalışan fixture'lar var. |
| PaySim replay | Tam hibrit PR-AUC **0,3919** [0,3463; 0,4417]; kurallar tek başına 0,0462. Graf katmanı ve anomali girdisi PaySim'de **katkı yapmıyor**. |
| Sentetik veri | Parmak izleri ve kâhin step-up geri bildirimi kaldırıldı. GBM PR-AUC **0,8423** [0,7929; 0,8865]. Eski 0,971'in bir sızıntıdan geldiği ölçüldü. |
| Elliptic | LightGBM `all` illicit F1 **0,7984** [0,7798; 0,817]; karşılaştırma için Weber vd. 2019, RF AF 0,788. Yapısal graf özellikleri tek başına zayıf (F1 0,1177) ve `all`'a bir şey eklemiyor. |
| Performans | HTTP tek istemci p99 **31–35 ms** (önce 265–523 ms). Eşzamanlı: 50 TPS'te p99 472 ms (önce 1.906 ms). |
| Kalite | Sonuçlar §5'te. |

## 2. Bulgular: commit ve test eşlemesi

Her bulgu önce `tests/test_audit_v2.py` içinde strict `xfail` ile yeniden üretildi (`ea2f37b`),
düzeltme commit'inde işaret kaldırıldı. Test adları o dosyadandır, aksi belirtilmedikçe.

### A. Doğruluk ve eğitim/canlı paritesi

| # | Bulgu | Commit | Test |
|---|---|---|---|
| A1 | Demo/fallback çıktısı şemayı bozuyor, ŞİB taslağı sessizce kayboluyor | `cd90d55` | `test_a1_demo_sib_and_summary_valid_without_persisted_tx`, `test_a1_rejected_copilot_output_is_recorded_not_silent` |
| A2 | STEP_UP'ı geçen meşru cihaz öğrenilmiyor | `41bc790` | `test_a2_passed_step_up_teaches_the_new_device` |
| A3 | Backfill ile canlı profil öğrenme kuralı farklı (train/serve skew) | `658cd1b` | `test_a3_backfill_and_live_replay_produce_identical_features` |
| A4 | Düşük riskte kural tabanı HOLD'a zorluyor | `32bcffb` | `test_a4_second_transfer_of_the_day_is_step_up_not_hold` |
| A5 | Kart testi ATO olarak tipleniyor | `cf01af2` | `test_a5_card_testing_opens_card_testing_case` |
| A6 | Fan-in mağduru halkaya giriyor; BLOCK sonrası `fraud_nodes` 0 | `b5b74dd` | `test_a6_fan_in_victims_are_affected_not_ring_members`, `test_a6_block_flags_graph_fraud_nodes` |
| A7 | PSI drift açılışta yanlış alarm veriyor | `be5b1e7` | `test_a7_no_drift_alarm_on_demo_startup_but_real_drift_alarms`, `test_a7_psi_handles_empty_bins_and_windows` |

### B. Eşzamanlılık ve dayanıklılık

| # | Bulgu | Commit | Test |
|---|---|---|---|
| B8 | Snapshot ve commit ayrı; eşzamanlı velocity eksik sayılıyor | `73e7c9e` | `test_b8_concurrent_same_customer_velocity_is_exact` |
| B9 | Sınırsız büyüyen Redis hash'leri | `73e7c9e` | `test_b9_every_feature_store_key_has_a_ttl` |
| B10 | Redis Streams idempotency atomik değil | `73e7c9e` | `test_b10_replayed_commit_does_not_double_count`, `test_b10_redis_commit_is_atomic_and_idempotent`, `test_b10_stream_message_in_progress_is_not_processed_twice` |
| B11 | HTTP ingest gecikmesi | `5b30044`, `4a91075`, `56c86f1`, `2e92888`, `8ae8cf7`, `f0762cc` | `tests/test_ingest_path.py` (10 test: Bloom filtresi, DB'siz yeni işlem, yan etkilerin yanıtı beklememesi, çok worker'lı idempotency ve SSE bileti) |
| B12 | Sıraya bağlı simülatör testi | `94536d6` | `tests/test_simulator.py::test_run_publishes_traffic_and_scenarios` + CI'da `-p randomly` (`def6571`) |
| B13 | Tatil takvimi yalnızca 2026 | `507eda8` | `test_b13_masak_deadline_skips_2027_and_2028_religious_holidays`, `test_b13_arife_half_day_policy_is_configurable` |
| B14 | Yüksek riskli ülke listesi FATF ile uyuşmuyor | `ed2e63f` | `test_b14_high_risk_countries_come_from_versioned_fatf_list` |
| B15 | Yaptırım taraması tek token/ikincil anahtar | `ed2e63f` | `test_b15_single_token_alias_and_secondary_keys` |

### C. Güvenlik

| # | Bulgu | Commit | Test |
|---|---|---|---|
| C16 | Demo kullanıcılar her ortamda | `a0d55ba` | `test_c16_prod_refuses_demo_users`, `test_c16_login_page_hides_demo_passwords_without_demo_users` |
| C17 | SSE'de JWT sorgu dizesinde | `a0d55ba`, `4a91075` | `test_c17_sse_uses_single_use_ticket`, `test_c17_credentials_in_query_strings_are_masked_in_logs`, `tests/test_ingest_path.py::test_sse_ticket_is_shared_across_workers_and_single_use` |
| C18 | `/metrics` herkese açık | `a0d55ba` | `test_c18_metrics_not_public_by_default` |
| C19 | Ingest rate limit 200.000/dk, HMAC tekrar oynatılabilir | `a0d55ba` | `test_c19_ingest_limit_is_per_client_and_hmac_nonce_blocks_replay`, `test_c19_rate_limit_bucket_is_per_client` |
| C20 | ASCII'ye katlanmış isimler maskelenmiyor | `843de6b` | `test_c20_redaction_handles_ascii_folding_and_beneficiaries` |
| C21 | İç LLM sunucu adresi repoda | `7f0b806` | `test_c21_no_internal_llm_host_in_repository`, `test_c21_llm_status_hides_host_from_non_admins` |

### D. Arayüz

| # | Bulgu | Commit | Test |
|---|---|---|---|
| D22 | Frontend testsiz | `32781ed` | `frontend/src/test/{Cases,CaseDetail,Rules,Validation}.test.tsx` (8 Vitest testi), `frontend/e2e/analyst-flow.spec.ts` (Playwright, CI `e2e` işi) |
| D23 | Erişilebilirlik | `32781ed` | Vitest testleri rol/etiket ile sorgular; graf ve grafikler için tablo alternatifi |
| D24 | Ekran görüntüsü yok | `32781ed` | `scripts/screenshots.py` → `docs/img/*.png`, README'de |
| D25 | Sunucu tarafı sayfalama yok | `fec3395`, `32781ed` | `tests/test_cases_api.py::TestPagination`, `Cases.test.tsx` |

### E. Dürüstlük ve dokümantasyon

| # | Bulgu | Commit | Kanıt |
|---|---|---|---|
| E26 | README'de eşitlik tablosu | `60b2ab8` | "İlham alınan desenler" ve "Sınırlamalar" bölümleri |
| E27 | Performans iddiaları etiketsiz | `60b2ab8`, `f0762cc`, `e625b7a` | README "motor" etiketi; PERFORMANCE.md'de motor / HTTP tek istemci / HTTP eşzamanlı ayrı tablolar |
| E28 | Statik rozetler | `60b2ab8` | Yalnızca CI'dan üretilen durum rozeti kaldı |
| E29 | Doğrulanmamış mevzuat atıfları | `13cc1ac` | Her atıf resmî URL'ye bağlı; doğrulanamayan FATF gri liste maddeleri işaretli |
| E30 | Rapordaki araç meta cümlesi | `3274c55` | Cümle kaldırıldı |
| E31 | Bu rapor | bu commit | — |

### F. Bağımsız denetim turu 1 (2.1 adayı)

Denetçi, kodu okumanın yanında sistemi de ayrı bir portta ve geçici bir DB ile çalıştırdı. PaySim
fixture'ını kendi betiğiyle yeniden değerlendirdi. Bulgular üç kolda kapatıldı: güvenlik,
dayanıklılık ve ML. Güvenlik ve dayanıklılık dalları `integrate` dalında birleştirildi.
Birleştirmeden sonra kurallar ve eşikler, maker-checker onayından sonra `ConfigSync` üzerinden
bütün worker'lara yayınlanıyor.

| # | Bulgu | Commit |
|---|---|---|
| H1 | Step-up sonucu sahtelenebiliyor veya tekrar oynatılabiliyor | `243d695` |
| H2 | Analist üretime işlem enjekte edebiliyor | `243d695` |
| H3 | Çok worker'lı durum tutarsız (hesap durumu, eşikler, kurallar, model) | `739f4d7`, `7d4aeb8` |
| H4 | Yazıcıda veri kaybı; ingress mesajı commit'ten önce ack'leniyor | `98c6f10`, `4f08437` |
| H5 | Break-glass `ADMIN_TOKEN` dört gözü deliyor | `0ce8d72` |
| H6 | Compose dev modunda ve demo kimlik bilgileriyle geliyor | `59a2edd` |
| H7 | ML değerlendirmesi iddialardan zayıf | `594b29c`, `2f12beb`, `4fc321f`, `db2c049`, `c2ed9d6`, `cf17a7f` |
| M1 | Idempotency yük çakışmasını görmüyor | `503122c` |
| M2 | Onayda satır kilidi yok; işleyici commit'ten sonra çalışıyor | `0ce8d72` |
| M3 | Onay türüne göre en düşük rol yok | `0ce8d72` |
| M4 | Kural ve eşik değişiklikleri maker-checker'ı atlıyor | `5f89d3e` |
| M5 | Tek analist vakayı FRAUD olarak kapatabiliyor | `345c9ca` |
| M6 | Kural simülatörü ile DoS | `b5d4491` |
| M7 | Hız sınırı boşlukları | `243d695`, `034171f` |
| M8 | JWT yaşam döngüsü zayıf | `a21eaef` |
| M9 | Audit zinciri yeniden yazılabilir | `385dd5a` (isteğe bağlı HMAC anahtarı, doğrulama hatası metriği) |
| M10 | Hesap durumu compare-and-set değil | `739f4d7` |
| M11 | PII sızıntısı (maskeleme, DLQ, copilot araçları) | `bb7b1a2`, `fecd864`, `3d8b402` |
| M12 | Feature kilidi açık başarısız oluyor | `be02b2e` (kilit alınamazsa HOLD) |
| M13 | Yan etkiler kaybolabiliyor | `998bf36` (vaka alımı outbox) |
| M14 | Champion seçimi test verisine bakıyor | `9255485`, `9b4bf9a` |
| M15 | Eğitim/sunum farkı (kâhin step-up, PaySim saat içi dağılım) | `7acc23f`, `ea6b664` |
| M16 | Eski veya tutarsız dokümantasyon | `8affd77`, `1aaca6d` |
| M17 | Tedarik zinciri ve yapılandırma | `59a2edd`, `ab59e74` |
| L1 | Senaryo seçici BLOKE hesapları dışlamıyor | `ba76fdc` |
| L2 | `load_test.py --help` cp1254 konsolda çöküyor | `7b646a9` |
| L3 | CSV audit dışa aktarımında formül enjeksiyonu | `034171f` |
| L4 | `/health` sürüm bilgisi sızdırıyor; bearer öneki büyük/küçük harfe duyarlı | `034171f` |
| L5 | `sla_scan` süresi geçmiş MASAK vakalarını "yakında" sayıyor | `6ffcc93` |
| L6 | Aynı türden birden çok bekleyen onay mümkün | `0ce8d72` (kısmi tekil indeks, migration 0003) |
| L7 | Sohbet sorusu GET sorgu dizesinde ve loglanıyor | `034171f` |
| L8 | Kullanıcı adlı `Content-Disposition` 500 verebiliyor | `034171f` |
| L9 | Sentetik veride amaç/IP/alıcı adı parmak izi | `c85cad4` |
| L10 | PaySim adaptöründe `index % 3600` zaman kaydırması | `ea6b664` |
| L11 | Tabakasız 200 turluk bootstrap, sihirli sayılar | `594b29c` |
| L12 | İç LLM sunucu adı git geçmişinde; kodda ağ geçidi markası | `caacaec` (yalnız kod ve doküman) |
| L13 | e2e CI işi `continue-on-error` | `ab59e74` |

**Açık kalan: L12'nin git geçmişi kısmı.** Adres `7f0b806`'dan beri çalışma ağacında yok. Geçmişi
yeniden yazmak için `main`'e force push gerekir; bu da mevcut klonları ve `v2.0.0` etiketini
bozar. Bu yüzden geçmiş değiştirilmedi. Adres bir sır değildir, kimlik doğrulaması olmadan
erişilemeyen bir iç sunucu adıdır. Kodda eski ortam değişkeni adı, mevcut `.env` dosyaları
çalışmaya devam etsin diye dokümante edilmeden kabul ediliyor.

**Birleştirme sırasında değişen iki test.** Kalibre edilmiş v5 stacker, bir kural tabanına
takılan işleme yaklaşık 0,5 skor veriyor. Bu yüzden `tests/test_scoring_engine.py` ve
`tests/test_pipeline.py` sabit bir skor eşiğini değil, sonucu doğruluyor: karar `BLOCK` ve
politika gerekçesinde `RULE_FLOOR` var.

## 3. Gerçek veri doğrulaması

Yöntem: bütün işlemler canlı yolla aynı motordan (kurallar, feature store, politika) geçirilir,
zamana göre bölünür (eğitim / doğrulama / test), metrikler test döneminde bootstrap güven
aralıklarıyla verilir. Ayrıntı: `VALIDATION_REPORT.md` §2.

### 3.1 PaySim (%10 alıcı-hash örneği, 743 adım, 70/15/15 satır bölmesi, test 95.634 işlem / 382 fraud)

| Katman | PR-AUC [95 % GA] | ROC-AUC | Recall @ 1 % FPR |
|---|---|---|---|
| `rules` | 0,0462 | 0,7412 | 0,0681 |
| `gbm` | 0,3732 | 0,9125 | 0,5995 |
| `rules+gbm` (= `+anomaly` = `+graph` = `full`) | **0,3919** [0,3463; 0,4417] | 0,8721 | 0,5864 |

Son dört katman PaySim'de aynı skoru ürettiği için tek satırda gösterildi; güven aralığı `full`
katmanının bootstrap'ından alındı ve bu dört katmanın her biri için aynıdır. Stacker PR-AUC'yi
artırırken ROC-AUC'yi (0,9125 → 0,8721) ve 1 % FPR'deki recall'ı (0,5995 → 0,5864) düşürüyor:
kazanç yalnız en üst sıralardaki kesinlikte, sıralamanın geri kalanında GBM tek başına daha iyi.

1 % alarm bütçesinde recall 0,5471 [0,5026; 0,5975], precision 0,2186 ve tutar ağırlıklı recall
0,907. Test dönemindeki 382 fraud işleminin 280'i ALLOW aldı. Eşleştirilmiş bootstrap ile
`rules+gbm` − `rules` = +0,3457 [0,3045; 0,3885]. Stacker, GBM'e +0,0187 [0,0124; 0,025] ekliyor.
Stacker ablation'ı anomali girdisini düşürdü. Graf katmanının katkısı yok, çünkü PaySim'de cihaz,
IP ve alıcılar arası para akışı yok.

**Önceki sürümle karşılaştırma.** Eski raporda PaySim `full` 0,4968 idi, ama o sayı zaman aralığı
bölmesiyle 28.124 satırlık bir test kümesinde ölçülmüştü. Yeni sayı 95.634 satırlık satır
bölmesinden, kâhin olmayan step-up modeliyle ve eşitliklere duyarlı bütçeyle geliyor. İki sayı
doğrudan karşılaştırılamaz.

### 3.2 Elliptic (zaman adımı başına tümevarımsal graf özellikleri)

| Özellik kümesi | Illicit F1 [95 % GA] |
|---|---|
| `graph` (4 yapısal özellik) | 0,1177 |
| `all` (165) | **0,7984** [0,7798; 0,817] |
| `all+graph` (169) | 0,7891 [0,7705; 0,8065] |
| Weber vd. 2019, RF (AF) | 0,788 |

İlk denemede kullanılan "bilinen illicit düğüme uzaklık" özelliği etiket sızdırıyordu. Bu özellik
kaldırıldı ve `tests/test_public_data.py::test_elliptic_graph_features_are_label_free` ile test
ediliyor. Graf özellikleri artık her zaman adımında yalnızca o adıma kadar görülen grafla
hesaplanıyor.

### 3.3 ULB kredi kartı (yalnızca ML hattı, PCA özellikleri)

| Skor | PR-AUC [95 % GA] |
|---|---|
| `gbm` | **0,7335** [0,6092; 0,8451] |
| `hybrid` | 0,7091 [0,5794; 0,841] |

`hybrid` − `gbm` farkının güven aralığı [−0,0674; 0,0143], yani "katkı yok". ULB'de kurallar,
feature store ve graf uygulanamaz. Bu kontrol yalnızca GBM, anomali, stacker ve kalibrasyon
hattını gerçek kart verisinde sınar.

### 3.4 Sentetik ile gerçek; kalibrasyon

| Katman | Sentetik PR-AUC | PaySim PR-AUC |
|---|---|---|
| `gbm` | 0,8423 [0,7929; 0,8865] | 0,3732 |
| `full` | 0,8295 | 0,3919 |
| Eski `fraud_gbm_v1` (parmak izli üretici) | 0,971 | — |

Sentetik veride politika katmanı (`full`) PR-AUC'yi düşürüyor: Δ −0,0118 [−0,0196; −0,0046].
Bu bir sıralama metriği için zarar; bir karar kuralı olarak ise bilinçli bir tercih. Sentetik
sayı, temizlenmiş üreticinin kolay olduğunu gösterir; gerçek dünya performansı iddiası değildir.

| Kalibrasyon (test) | Brier | ECE (10 kutu) |
|---|---|---|
| Sentetik GBM | 0,00848 | 0,00834 |
| Sentetik stacker | 0,00649 | 0,00378 |
| PaySim GBM | 0,00309 | 0,00209 |
| PaySim stacker | 0,00308 | 0,00230 |

`+graph` katmanındaki noisy-OR birleşimi kalibrasyonu bozuyor (ECE ≈ 0,08). Bu skor olasılık
olarak okunmamalı.

### 3.5 Champion kararı

`fraud_gbm_v5` champion, `fraud_gbm_v6` challenger oldu. Seçim yalnızca doğrulama dönemine
bakarak yapıldı: 1 % bütçede kaçan fraud tutarı payı v5'te 0,1449, v6'da 0,1982. Test bir kez ve
seçimden sonra raporlandı: v5 PR-AUC 0,8355, v6 0,8401. Test sonucu seçimi değiştirmedi. Terfi dört
göz kuralıyla yapıldı. v1–v4 arşivlendi. Kanıt: `artifacts/validation/champion_selection.json`.
İki aday da PaySim'e yeniden eğitim olmadan aktarıldığında zayıf (PR-AUC 0,0119 / 0,0122).
Sentetik davranış özellikleri PaySim'e taşınmıyor; bu yüzden PaySim sonuçları o veride eğitilen
modelden gelir.

## 4. Performans (öncesi / sonrası)

| Ölçüm | Önce | Sonra |
|---|---|---|
| Motor p99 (süreç içi) | 16,9 ms | 7,1 ms (kod aynı; fark ölçüm koşulu) |
| HTTP tek istemci p99 (dev, tek süreç, SQLite) | 265 ms (denetim), 523 ms (yeniden ölçüm) | **31,5–34,7 ms** |
| HTTP eşzamanlı, 50 TPS hedef, 32 istemci, p99 | 1.906 ms | 472 ms |
| HTTP eşzamanlı, 100 TPS hedef, p99 | — | 862 ms |
| Docker (Postgres + Redis, 4 worker), en iyi | — | 129 TPS, p99 2,1 s |
| Motor yük testi, dayanıklılık düzeltmelerinden (H4, M13) sonra | 264,2 TPS, p99 9,3 ms | 417,5 TPS, p99 4,2 ms |

Değişiklikler: karar yolunda yalnızca skor, karar ve idempotency senkron kaldı; vaka/alert,
SSE ve dış çıkış sınırlı bir write-behind kuyruğuna taşındı (geri basınç, kayıp yok). Yeni işlem
kimliği için DB sorgusu yerine ölçeklenebilir Bloom filtresi; Redis'te `SET NX` ile worker'lar
arası idempotency. SSE biletleri ve rate limit Redis'te paylaşılıyor.

**Karşılanamayan hedef:** 500 TPS / p99 < 150 ms. İşlem başına ~11 ms CPU (LightGBM + SHAP,
IForest/ECOD, özellikler, yaptırım), 4 çekirdekte teorik tavan ~360 TPS; Postgres, Redis ve yük
istemcisi aynı çekirdekleri paylaşıyor. Demo popülasyonu yüksek hızda velocity fraud gibi
göründüğü için karar karışımı da gerçekçi değil (%48 BLOCK). Daha büyük popülasyonla yapılacak
ölçüm Docker Desktop yanıt vermediği için tamamlanamadı. Ayrıntı: `PERFORMANCE.md`.

## 5. Kalite kapıları

| Kapı | Sonuç |
|---|---|
| `ruff check`, `ruff format --check` | Geçti (193 dosya). |
| `mypy app` | Geçti, 133 kaynak dosyada hata yok. |
| pytest, rastgele sıra, coverage | `c55eb14` üzerinde tur 1 (seed 240473429): 665 test geçti, coverage **%93** (eşik %90), 10 dk 21 sn. Tur 2 (seed 918273645): 665 test geçti, coverage **%92**, 17 dk 3 sn. Tur 3 (seed 55512377) testlerin yaklaşık %90'ında makinede bellek yetersizliği nedeniyle durduruldu; o noktaya kadar hata yoktu, tur tamamlanmadı. Önceki rastgele turlarda görülen tek kararsız test (R11) düzeltildi. |
| `npm run build`, `npm test` | `bc9b939` üzerinde geçti (build 17,3 sn; 4 dosyada 9 test). Arayüz kodu o commit'ten sonra değişmedi. |
| `docker compose build` | `bc9b939` üzerinde geçti (yer tutucu `JWT_SECRET`, `AUDIT_HMAC_KEY`, `CONSORTIUM_SALT` ile). `USERS_FILE` eklendikten sonra `docker compose config` geçti. |

## 6. Bağımsız denetim turları

| Tur | Kapsam | Puan | Sonuç |
|---|---|---|---|
| 1 | Kod, çalışan sistem, PaySim yeniden değerlendirmesi | 7/10 | 37 bulgu; §2.F |
| 2 | Tur 1 düzeltmeleri, canlı sistem (ayrı port, geçici DB) | 7,5/10 | Tur 1'in 23 bulgusu kapalı, 14'ü kısmi. 25 yeni bulgu: 2 yüksek (A1 compose'un dev modunda açılması, A2 yazıcının sonsuza dek yeniden denemesi), 12 orta (A3–A14), 11 düşük (L1–L11). Hepsi üç dalda düzeltildi ve birleştirildi: `07a4e10` (ML, drift, kilit fencing), `d95a01e` (güvenlik, dağıtım), `bc9b939` (dayanıklılık, iş akışı). |
| 3 | Tur 2 düzeltmeleri, canlı sistem | 8,0/10 | İzlenen 39 maddeden 26'sı kapalı, 11'i kısmi, 2'si açık. Yeni bulgular: N1–N3 orta, N4–N5 düşük/orta, N6 doküman; düşük artıklar R1–R13. |

Denetim en fazla üç tur olarak planlandı. 9/10 hedefine ulaşılamadı; son puan tur 3'ün 8,0/10'udur.
Tur 3'ten sonra aşağıdaki düzeltmeler yapıldı; bunlar bağımsız bir turda yeniden puanlanmadı.

| Bulgu | Düzeltme | Commit |
|---|---|---|
| N1, N2 | Kendine atama TEMIZ kapanışındaki kıdem kapısını aşamıyor; başkasının vakasını devralmak kıdemli analist istiyor; FRAUD olmayan vakada ŞİB gönderimi reddediliyor. | `9e53ba3` |
| N3 | Prod'da demo kullanıcıları kapalıyken maker-checker için hash'li `USERS_FILE` hesapları (`scripts/create_user.py`). | `63a65ef` |
| N4 | Feature kilidi yenilenirken veya bırakılırken oluşan Redis hataları kilidin içinde ele alınıyor. | `ed0266f` |
| N5 | Challenge deposu erişilemezken tekrar gönderilen istekte saklanan karar dönüyor. | `eda9a68` |
| R1 | Saat dilimi belirtilmemiş `ts` UTC kabul ediliyor; aynı an için yanlış 409 dönmüyor. | `c55eb14` |
| R11 | Zamana bağlı idempotency testi, bekleme süresinin üzerinde bir kilit süresiyle kararlı. | `072ebe7` |
| A13 artığı | v6 model kartı, ADR 0003 ve `policy.py` artık ham LightGBM çıktısını kalibre olarak tanımlamıyor. | `75f6fe6` |
| N6, A12 | Bu bölüm, §5 ve `v2.1.0` etiketi. | — |

### Açık kalan maddeler ve gerekçeleri

| Madde | Durum | Gerekçe |
|---|---|---|
| A8, R12 | Kısıtlama yalnız IP'ye göre | Yalnız gerçek tahminler sayılıyor ve geçerli oturumlar kilitlenmiyor. Aynı NAT arkasındaki kullanıcılar 31 hatalı denemeden sonra 60 sn bekler. Yalnız kullanıcı adına göre anahtar, hesap kilitleme saldırısına kapı açar; IP ve kullanıcı adının birlikte kullanıldığı bir tasarım sonraki sürüme bırakıldı. API anahtarları en az 32 karakter olduğu için R12'nin etkisi ihmal edilebilir. |
| M5 | Tek kıdemli analistin FRAUD kapanışı konsorsiyuma yayımlanıyor | Konsorsiyuma yalnız tuzlanmış karşı taraf özeti gidiyor ve FRAUD kapanışı zaten kıdemli analist istiyor. Yayını ayrıca dört göz onayına bağlamak iş akışı kararıdır; kurumla netleşmeden eklenmedi. |
| M8 | Oturum süresi 60 dk, toplu iptal yok | `iss`/`aud` zorunlu, tekil iptal ve SSE yeniden kontrolü var. Süre `JWT_TTL_MINUTES` ile kısaltılabilir. Toplu iptal için `JWT_SECRET` döndürülebilir; bu tüm oturumları düşürür. |
| M9 | Audit HMAC'inde anahtar kimliği ve döndürme yok | Anahtar prod'da zorunlu ve zincir doğrulaması izleniyor. Döndürme şu an zincirin kesilip yeni anahtarla yeniden başlatılmasıyla yapılabilir; anahtar kimliği şema değişikliği gerektirir ve sonraki sürüme bırakıldı. |
| L3, R3 | Maskeleme `/` ve `_` ayraçlı kart numaralarını ve Batılı adları kaçırıyor | Türkçe adlar, IBAN, TCKN, telefon ve yaygın kart biçimleri maskeleniyor. Maskeleme LLM'e giden metin için ikinci savunma hattı; alan düzeyinde pseudonimleştirme birinci hat. Genel ad tespiti bir NER modeli gerektirir. |
| R4 | Güvenilmeyen veri etiketi sıfır genişlikli ve tam genişlikli varyantları kaçırıyor | Etiketin etkisizleştirilmesi yalnız bir katman. Copilot araçları vakanın müşterisiyle sınırlı ve çıktılar şema doğrulamasından geçiyor. Unicode normalizasyonu bir sonraki sürümde. |
| H7b, L8 | Step-up sonuç modeli varsayılan oranlarla | Oranlar MODEL_CARD'da açıkça belirtildi. Gerçek OTP sonuç verisi olmadan duyarlılık analizi varsayımı değiştirmez. Gerçek veride bu model gözlenen sonuçlarla değiştirilmeli. |
| L6 | Champion seçim metriği örneklem içi ve gürültülü | Stacker doğrulama diliminde eğitiliyor, GBM de erken durmayı orada yapıyor. Seçim 126 pozitifle ve güven aralığı olmadan veriliyor. Test sonucu seçimden sonra bir kez raporlanıyor: v6 test metriklerinde v5'ten biraz iyi (PR-AUC 0,8401 / 0,8355). Seçimi teste göre değiştirmek testi seçime katmak olurdu, bu yüzden v5 champion kaldı. Ayrı bir seçim dilimi ve bootstrap aralığı sonraki eğitim turunda eklenecek. |
| M15 | Eğitim/canlı çarpıklığı | MODEL_CARD'da açıkça belirtildi. Backfill ve canlı sistem aynı öğrenme kuralını paylaşıyor. Kalan fark sentetik geçmişin kısalığından geliyor. |
| L12 | İç LLM sunucu adı git geçmişinde | Kod ve dokümanlardan kaldırıldı; eski ortam değişkeni adı geriye uyumluluk için kabul ediliyor. Geçmişi yeniden yazmak force push gerektirir; proje kuralı buna izin vermiyor. |
| R2 | Challenge tüketildikten sonraki tekrar isteği STEP_UP ve boş challenge döndürüyor | Asıl sonuç `step-up-result` uç noktasında kaydediliyor. Tekrar isteği ilk kararı döndürür; sonucu yansıtmaması tasarım gereği. |
| R5 | Takılan yazıcı onayları bekletiyor | Onay, transaction'dan önce yazıcının boşalmasını bekliyor; yazıcı takılırsa onay 503 dönmek yerine bekliyor. Yazıcı hataları sınıflandırıldığı ve yeniden deneme sayısı sınırlı olduğu için bu yalnız DB yanıt vermezken olur. Onay yolunda zaman aşımı ve `chain.lock`'un daraltılması sonraki sürüme bırakıldı. |
| R6, R7 | Bölme yeniden deneme pencereleri toplanıyor; yerel uygulama sırası | Geçici görünen partiye özgü bir hata DLQ'ya düşmeyi ve ack'i yaklaşık 4,5 dk geciktirebiliyor. Bölme, bir işlemin satırlarını karar ve audit satırlarından ayırabiliyor (tur 2'den önce de vardı); ayrılan satırlar DLQ'da kalıyor ve `/api/bus/dlq` üzerinden görülebiliyor, ancak otomatik yeniden işleme yok. İşlem bazında gruplanmış bölme sonraki sürüme bırakıldı. |
| R8 | METRICS_TOKEN olmadan audit doğrulama alarmı sürekli çalıyor | `METRICS_TOKEN` yokken `/metrics` 404 döner ve Prometheus metrik toplayamaz; monitoring profili token ile çalıştırılmalıdır. Tokensiz kurulumda alarmın susturulması sonraki sürüme bırakıldı. |
| R9 | Migration 0005 yeniden sayımı | Kod okumasıyla bulundu; yalnız 0005 öncesi yinelenen bekleyen onayı olan kurulumları etkiler. Temiz kurulumda geçerli değil. |
| R10 | Çevrimiçi profil öğrenmesi TEMIZ etiketini nadiren görüyor | Tek analistin temiz etiketi bilerek öğrenilmiyor (A7). Çevrimdışı etiket satırları etkilenmiyor ve yeniden eğitimde kullanılıyor. |
| R13 | Küçük skorlama boşlukları | Etkisi düşük; sonraki model sürümünde ele alınacak. |
| Diğer | Varsayılan `ENVIRONMENT=dev` (kodda), Postgres parolası ve imajların digest ile sabitlenmemesi | Compose ve imaj varsayılan olarak prod modunda açılıyor. Digest sabitleme dağıtım hattına bırakıldı. |
