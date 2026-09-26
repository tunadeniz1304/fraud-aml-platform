# Model kartı — `fraud_gbm_v6`

## Amaç ve kapsam
Gerçek zamanlı giden transferlerde (FAST/EFT/havale/kart) dolandırıcılık olasılığı üretir. Karar **vermez**: çıktısı kural skoru ve anomali skoru ile birlikte lojistik stacker'a, oradan politika katmanına (ALLOW / STEP_UP / HOLD / BLOCK) gider. Yaptırım taraması, hesap durumu ve kural aksiyon tabanları deterministik override'lardır.

## Veri
- Sentetik, seed'li Türk bankacılığı verisi (seed=7); 500 müşteri, 63435 işlem, fraud oranı 0.0146.
- Tipolojiler: app: 70, ato: 108, card_testing: 298, mule: 407, normal: 62448, sanctions: 10, structuring: 94 (yaptırım isabetleri ML eğitiminden hariç).
- Zaman sıralı satır bölmesi 70/15/15 — eğitim 44397 (569 fraud), doğrulama 9514 (126), test 9514 (221); test dönemi 2026-07-22T00:22:22 → 2026-07-30T23:52:35.
- Feature'lar online skorlayıcıyla **aynı** motorla (feature store → kurallar → politika) kronolojik replay ile üretildi; profil öğrenmesi canlı sistemle aynı `should_learn` kuralını kullanır (parite testi: `test_a3_*`). Replay'de champion model yoktur; modelin ALLOW sınırını değiştirdiği olaylar kalan kaymadır.

## Test metrikleri
| Bileşen | PR-AUC | ROC-AUC | Recall @ %1 FPR | Kesinlik @ %1 bütçe | Maliyet ağırlıklı recall @ %1 bütçe |
|---|---|---|---|---|---|
| Kural motoru | 0.599 | 0.879 | 0.656 | 0.863 | 0.480 |
| LightGBM | 0.846 | 0.978 | 0.864 | 0.937 | 0.550 |
| Anomali (IForest+ECOD) | 0.522 | 0.917 | 0.557 | 0.758 | 0.323 |
| **Hibrit (stacker)** | 0.840 | 0.978 | 0.855 | 0.926 | 0.535 |

Maliyet ağırlıklı recall: işlemlerin en riskli %1'i alert olduğunda yakalanan fraud **tutarının** toplam fraud tutarına oranı.

## Kalibrasyon (test dönemi)
| Çıktı | Brier | ECE (10 kutu) | Ort. tahmin | Gözlenen oran |
|---|---|---|---|---|
| LightGBM (ham çıktı) | 0.00821 | 0.00792 | 0.0153 | 0.0232 |
| Hibrit (stacker) | 0.00626 | 0.00375 | 0.0208 | 0.0232 |

Kalibrasyon doğrulama dağılımında yapılır; test döneminde taban oran kayarsa ECE büyür. Etiket gürültüsü (kaçan fraud) gözlenen oranı gerçek oranın altında tutar.

## Tipoloji bazında politika sonuçları (test, harici sinyaller hariç)
| Tipoloji | Adet | ALLOW | STEP_UP | HOLD | BLOCK |
|---|---|---|---|---|---|
| app | 17 | 0 | 0 | 4 | 13 |
| ato | 18 | 3 | 2 | 4 | 9 |
| card_testing | 101 | 0 | 0 | 10 | 91 |
| mule | 86 | 45 | 8 | 17 | 16 |
| normal | 9278 | 9274 | 4 | 0 | 0 |
| structuring | 14 | 0 | 0 | 1 | 13 |

Eşikler: {'step_up': 0.35, 'hold': 0.6, 'block': 0.85}.

## En etkili feature'lar (LightGBM gain)
| Feature | Açıklama | Pay |
|---|---|---|
| `amount_ratio` | Tutarın müşterinin adaptif ortalamasına oranı | 0.159 |
| `paste_used` | Tutar/IBAN yapıştırıldı | 0.106 |
| `time_since_last_s` | Bir önceki işlemden bu yana geçen süre (sn) | 0.106 |
| `payee_relation_age_d` | Alıcıyla ilk işlemden bu yana gün (-1: yeni) | 0.090 |
| `amount_try` | İşlem tutarı (TRY'ye normalize) | 0.074 |
| `amount_zscore` | Tutarın müşterinin EWMA dağılımına göre z-skoru | 0.065 |
| `payee_age_d` | Alıcının sistemde ilk görülmesinden bu yana gün (-1: yeni) | 0.046 |
| `device_customers_7d` | Cihazı 7 günde kullanan farklı müşteri sayısı | 0.044 |
| `hour` | İşlem saati (0-23) | 0.040 |
| `login_to_transfer_s` | Oturum açmadan transfere kadar geçen süre (sn, -1 yok) | 0.038 |
| `night_ratio_7d` | Son 7 gündeki gece işlemlerinin oranı | 0.034 |
| `session_duration_s` | Oturum süresi (sn, -1 yok) | 0.025 |

## Açıklanabilirlik
Her kararda LightGBM `pred_contrib` (TreeSHAP; `shap.TreeExplainer` ile birebir aynı, testli) katkıları feature grubuna göre toplanıp `ML_*` reason code'larına eşlenir; kural isabetleri Türkçe şablonlu reason code üretir.

## Stacker
Girdi: logit(kural), logit(ML), logit(anomali); katsayılar [0.094593, 1.174001, 0.0], kesişim 1.818. Doğrulama bölmesinde negatif olmayan lojistik regresyon (çok değişkenli Platt): tek kısıt katsayı ≥ 0 (monotonluk — hiçbir bileşen riski düşüremez). Eski taban (≥ 0.05, kural ≥ 0.3) kaldırıldı; katsayısı 0 olan bileşen kullanılmıyor demektir. Anomali girdisi: dropped (validation ablation).

Ablasyon (validation first half -> fit, second half -> score (time order); satır [4757, 4757], pozitif [51, 75]):

| Varyant | Log-loss | Brier | PR-AUC | Katsayılar |
|---|---|---|---|---|
| `floored_legacy` | 0.02325 | 0.00455 | 0.820 | [0.3, 1.4364, 0.05] |
| `unconstrained` | 0.02098 | 0.00473 | 0.848 | [0.0638, 1.3295, -0.2848] |
| `nonneg` | 0.02135 | 0.00471 | 0.827 | [0.0374, 1.213, 0.0] |
| `nonneg_no_anomaly` | 0.02135 | 0.00471 | 0.827 | [0.0374, 1.213, 0.0] |

## Sınırlamalar ve riskler
- Sentetik veriyle eğitildi; gerçek dağılımlarda yeniden eğitim ve kalibrasyon şart.
- APP dolandırıcılığında cihaz/konum tanıdık olduğundan model sosyal mühendislik sinyallerine (görüşme, uzaktan erişim, metin) dayanır; bu sinyaller yoksa kaçırma olasılığı artar — graf ve CoP sinyalleri politika katmanında bunu telafi eder.
- Yaşlı/kırılgan müşteri bayrağı riski artırır; amaç koruyucu friction'dır (uyarı, bekletme), ret değil. Adillik izlemesi için karar oranları segment bazında izlenmeli.
- Drift: skor ve en etkili 10 feature için PSI referans dağılımları metadata'dadır.

Eğitim süresi: 55.1 sn · ağaç sayısı: 82.
