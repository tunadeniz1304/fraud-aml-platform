# Model kartı — `fraud_gbm_v1`

## Amaç ve kapsam
Gerçek zamanlı giden transferlerde (FAST/EFT/havale/kart) dolandırıcılık olasılığı üretir. Karar **vermez**: çıktısı kural skoru ve anomali skoru ile birlikte lojistik stacker'a, oradan politika katmanına (ALLOW / STEP_UP / HOLD / BLOCK) gider. Yaptırım taraması, hesap durumu ve kural aksiyon tabanları deterministik override'lardır.

## Veri
- Sentetik, seed'li Türk bankacılığı verisi (seed=42); 500 müşteri, 60760 işlem, fraud oranı 0.01618.
- Tipolojiler: app: 73, ato: 112, card_testing: 265, mule: 416, normal: 59777, sanctions: 10, structuring: 107 (yaptırım isabetleri ML eğitiminden hariç).
- Zaman bazlı bölme 70/15/15 — eğitim 42525 (686 fraud), doğrulama 9112 (93), test 9113 (194); test dönemi 2026-07-21T20:36:24 → 2026-07-30T23:57:39.
- Feature'lar online skorlayıcıyla **aynı** fonksiyonlarla kronolojik replay ile üretildi (eğitim/servis kayması yok).

## Test metrikleri
| Bileşen | PR-AUC | ROC-AUC | Recall @ %1 FPR | Kesinlik @ %1 bütçe | Maliyet ağırlıklı recall @ %1 bütçe |
|---|---|---|---|---|---|
| Kural motoru | 0.675 | 0.909 | 0.711 | 0.901 | 0.638 |
| LightGBM | 0.974 | 0.997 | 0.974 | 1.000 | 0.362 |
| Anomali (IForest+ECOD) | 0.747 | 0.975 | 0.727 | 0.934 | 0.680 |
| **Hibrit (stacker)** | 0.971 | 0.993 | 0.969 | 1.000 | 0.459 |

Maliyet ağırlıklı recall: işlemlerin en riskli %1'i alert olduğunda yakalanan fraud **tutarının** toplam fraud tutarına oranı.

## Tipoloji bazında politika sonuçları (test, harici sinyaller hariç)
| Tipoloji | Adet | ALLOW | STEP_UP | HOLD | BLOCK |
|---|---|---|---|---|---|
| app | 10 | 0 | 0 | 0 | 10 |
| ato | 23 | 1 | 0 | 0 | 22 |
| card_testing | 66 | 0 | 0 | 0 | 66 |
| mule | 72 | 13 | 1 | 2 | 56 |
| normal | 8919 | 8908 | 2 | 5 | 4 |
| structuring | 23 | 0 | 0 | 0 | 23 |

Eşikler: {'step_up': 0.35, 'hold': 0.6, 'block': 0.85}.

## En etkili feature'lar (LightGBM gain)
| Feature | Açıklama | Pay |
|---|---|---|
| `is_new_device` | Cihaz müşteri için yeni | 0.361 |
| `device_age_d` | Cihazın sistemde ilk görülmesinden bu yana gün | 0.175 |
| `amount_ratio` | Tutarın müşterinin adaptif ortalamasına oranı | 0.112 |
| `amount_zscore` | Tutarın müşterinin EWMA dağılımına göre z-skoru | 0.067 |
| `is_new_payee` | Bu alıcıya ilk kez para gönderiliyor | 0.036 |
| `payee_relation_age_d` | Alıcıyla ilk işlemden bu yana gün (-1: yeni) | 0.029 |
| `time_since_last_s` | Bir önceki işlemden bu yana geçen süre (sn) | 0.023 |
| `payee_age_d` | Alıcının sistemde ilk görülmesinden bu yana gün (-1: yeni) | 0.020 |
| `login_to_transfer_s` | Oturum açmadan transfere kadar geçen süre (sn, -1 yok) | 0.016 |
| `near_threshold` | Tutar raporlama eşiğinin hemen altında (yapılandırma) | 0.016 |
| `hour_unusual` | Saat müşterinin adaptif saat dağılımında nadir | 0.016 |
| `near_threshold_cnt_24h` | 24 saatte eşik altı işlem sayısı (bu işlem dahil) | 0.015 |

## Açıklanabilirlik
Her kararda LightGBM `pred_contrib` (TreeSHAP; `shap.TreeExplainer` ile birebir aynı, testli) katkıları feature grubuna göre toplanıp `ML_*` reason code'larına eşlenir; kural isabetleri Türkçe şablonlu reason code üretir.

## Stacker
Girdi: logit(kural), logit(ML), logit(anomali); katsayılar [0.3, 1.6273499528221458, 0.05], kesişim 3.471. Katsayılar ≥ 0.05 ile tabanlanır (monotonluk: hiçbir bileşen riski düşüremez).

## Sınırlamalar ve riskler
- Sentetik veriyle eğitildi; gerçek dağılımlarda yeniden eğitim ve kalibrasyon şart.
- APP dolandırıcılığında cihaz/konum tanıdık olduğundan model sosyal mühendislik sinyallerine (görüşme, uzaktan erişim, metin) dayanır; bu sinyaller yoksa kaçırma olasılığı artar — graf ve CoP sinyalleri politika katmanında bunu telafi eder.
- Yaşlı/kırılgan müşteri bayrağı riski artırır; amaç koruyucu friction'dır (uyarı, bekletme), ret değil. Adillik izlemesi için karar oranları segment bazında izlenmeli.
- Drift: skor ve en etkili 10 feature için PSI referans dağılımları metadata'dadır.

Eğitim süresi: 10.32 sn · ağaç sayısı: 175.

## Yönetişim (P1.6)
- **Champion / challenger:** `models/registry.json`; challenger (`fraud_gbm_v2`) her işlemde gölge skorlanır, karara etki etmez; `GET /api/models/compare` çevrimdışı (test) ve çevrimiçi (alert oranı, karar uyumu, etiketliyse PR-AUC ve maliyet: FP inceleme maliyeti + kaçan fraud tutarı) karşılaştırır.
- **Terfi:** admin talebi + farklı kıdemli kullanıcının onayı (maker-checker) → canlı motor yeniden yüklenir.
- **Drift:** skor ve en etkili 10 feature için PSI (0,10 izle / 0,25 alarm) — `fraud_drift_psi`, `GET /api/models/drift`.
- **Geri besleme:** analist kararları `labels` tablosuna yazılır; `scripts/train_models.py --incremental` yeni versiyon üretir; worker yeterli etiket biriktiğinde yeniden eğitim önerir; `GET /api/models/active-learning` belirsiz (0,4–0,6) işlemleri etiketleme için önerir.
