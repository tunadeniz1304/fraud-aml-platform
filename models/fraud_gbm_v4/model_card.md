# Model kartı — `fraud_gbm_v4`

## Amaç ve kapsam
Gerçek zamanlı giden transferlerde (FAST/EFT/havale/kart) dolandırıcılık olasılığı üretir. Karar **vermez**: çıktısı kural skoru ve anomali skoru ile birlikte lojistik stacker'a, oradan politika katmanına (ALLOW / STEP_UP / HOLD / BLOCK) gider. Yaptırım taraması, hesap durumu ve kural aksiyon tabanları deterministik override'lardır.

## Veri
- Sentetik, seed'li Türk bankacılığı verisi (seed=7); 500 müşteri, 62869 işlem, fraud oranı 0.02564.
- Tipolojiler: app: 74, ato: 117, card_testing: 278, mule: 419, normal: 61879, sanctions: 10, structuring: 92 (yaptırım isabetleri ML eğitiminden hariç).
- Zaman bazlı bölme 70/15/15 — eğitim 44001 (1064 fraud), doğrulama 9429 (284), test 9429 (254); test dönemi 2026-07-21T19:34:48 → 2026-07-31T03:34:40.
- Feature'lar online skorlayıcıyla **aynı** motorla (feature store → kurallar → politika) kronolojik replay ile üretildi; profil öğrenmesi canlı sistemle aynı `should_learn` kuralını kullanır (parite testi: `test_a3_*`). Replay'de champion model yoktur; modelin ALLOW sınırını değiştirdiği olaylar kalan kaymadır.

## Test metrikleri
| Bileşen | PR-AUC | ROC-AUC | Recall @ %1 FPR | Kesinlik @ %1 bütçe | Maliyet ağırlıklı recall @ %1 bütçe |
|---|---|---|---|---|---|
| Kural motoru | 0.314 | 0.722 | 0.319 | 0.660 | 0.600 |
| LightGBM | 0.567 | 0.783 | 0.539 | 0.989 | 0.577 |
| Anomali (IForest+ECOD) | 0.262 | 0.768 | 0.268 | 0.553 | 0.551 |
| **Hibrit (stacker)** | 0.556 | 0.785 | 0.531 | 0.989 | 0.656 |

Maliyet ağırlıklı recall: işlemlerin en riskli %1'i alert olduğunda yakalanan fraud **tutarının** toplam fraud tutarına oranı.

## Tipoloji bazında politika sonuçları (test, harici sinyaller hariç)
| Tipoloji | Adet | ALLOW | STEP_UP | HOLD | BLOCK |
|---|---|---|---|---|---|
| app | 6 | 0 | 0 | 0 | 6 |
| ato | 24 | 0 | 0 | 0 | 24 |
| card_testing | 32 | 0 | 0 | 1 | 31 |
| mule | 65 | 17 | 9 | 7 | 32 |
| normal | 9280 | 9244 | 28 | 7 | 1 |
| structuring | 22 | 0 | 0 | 0 | 22 |

Eşikler: {'step_up': 0.35, 'hold': 0.6, 'block': 0.85}.

## En etkili feature'lar (LightGBM gain)
| Feature | Açıklama | Pay |
|---|---|---|
| `amount_ratio` | Tutarın müşterinin adaptif ortalamasına oranı | 0.267 |
| `payee_age_d` | Alıcının sistemde ilk görülmesinden bu yana gün (-1: yeni) | 0.081 |
| `payee_relation_age_d` | Alıcıyla ilk işlemden bu yana gün (-1: yeni) | 0.070 |
| `amount_try` | İşlem tutarı (TRY'ye normalize) | 0.069 |
| `time_since_last_s` | Bir önceki işlemden bu yana geçen süre (sn) | 0.055 |
| `device_age_d` | Cihazın sistemde ilk görülmesinden bu yana gün (-1: cihaz yok) | 0.044 |
| `amount_zscore` | Tutarın müşterinin EWMA dağılımına göre z-skoru | 0.041 |
| `session_duration_s` | Oturum süresi (sn, -1 yok) | 0.038 |
| `login_to_transfer_s` | Oturum açmadan transfere kadar geçen süre (sn, -1 yok) | 0.037 |
| `sum_7d` | Son 7d içindeki toplam tutar (TRY) | 0.030 |
| `is_new_payee` | Bu alıcıya ilk kez para gönderiliyor | 0.028 |
| `sum_24h` | Son 24h içindeki toplam tutar (TRY) | 0.022 |

## Açıklanabilirlik
Her kararda LightGBM `pred_contrib` (TreeSHAP; `shap.TreeExplainer` ile birebir aynı, testli) katkıları feature grubuna göre toplanıp `ML_*` reason code'larına eşlenir; kural isabetleri Türkçe şablonlu reason code üretir.

## Stacker
Girdi: logit(kural), logit(ML), logit(anomali); katsayılar [0.3, 1.1814616407149336, 0.05], kesişim 2.074. Katsayılar ≥ 0.05 ile tabanlanır (monotonluk: hiçbir bileşen riski düşüremez).

## Sınırlamalar ve riskler
- Sentetik veriyle eğitildi; gerçek dağılımlarda yeniden eğitim ve kalibrasyon şart.
- APP dolandırıcılığında cihaz/konum tanıdık olduğundan model sosyal mühendislik sinyallerine (görüşme, uzaktan erişim, metin) dayanır; bu sinyaller yoksa kaçırma olasılığı artar — graf ve CoP sinyalleri politika katmanında bunu telafi eder.
- Yaşlı/kırılgan müşteri bayrağı riski artırır; amaç koruyucu friction'dır (uyarı, bekletme), ret değil. Adillik izlemesi için karar oranları segment bazında izlenmeli.
- Drift: skor ve en etkili 10 feature için PSI referans dağılımları metadata'dadır.

Eğitim süresi: 24.93 sn · ağaç sayısı: 142.
