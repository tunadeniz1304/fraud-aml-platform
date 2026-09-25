# Model kartı — `fraud_gbm_v5` (champion)

> Bu kart `models/fraud_gbm_v5/model_card.md` dosyasının kopyasıdır ve eğitim betiğinin ürettiği sayıları içerir. Champion `fraud_gbm_v5`, challenger `fraud_gbm_v6` (`models/registry.json`). İkisi de asimetrik etiket gürültüsü (kaçan fraud %10, yanlış fraud etiketi %0,05) ve kâhin olmayan step-up sonucu üreten sentetik üreticiyle eğitildi. `fraud_gbm_v1`–`v4` arşivlendi. v1/v2'nin 0,971/0,967 PR-AUC'si eski üreticinin etiketi cihaz kimliğine sızdırmasından geliyordu. v3/v4 ise simetrik %1 etiket gürültüsüyle ve kâhin step-up ile üretilmiş veriye aitti; sayıları buradakilerle karşılaştırılamaz (`docs/VALIDATION_REPORT.md` §1, §4).

## Amaç ve kapsam
Gerçek zamanlı giden transferlerde (FAST/EFT/havale/kart) dolandırıcılık olasılığı üretir. Karar **vermez**: çıktısı kural skoru ve anomali skoru ile birlikte lojistik stacker'a, oradan politika katmanına (ALLOW / STEP_UP / HOLD / BLOCK) gider. Yaptırım taraması, hesap durumu ve kural aksiyon tabanları deterministik override'lardır.

## Veri
- Sentetik, seed'li Türk bankacılığı verisi (seed=42); 500 müşteri, 63435 işlem, fraud oranı 0.0146.
- Tipolojiler: app: 70, ato: 108, card_testing: 298, mule: 407, normal: 62448, sanctions: 10, structuring: 94 (yaptırım isabetleri ML eğitiminden hariç).
- Zaman sıralı satır bölmesi 70/15/15 — eğitim 44397 (569 fraud), doğrulama 9514 (126), test 9514 (221); test dönemi 2026-07-22T00:22:22 → 2026-07-30T23:52:35.
- Feature'lar online skorlayıcıyla **aynı** motorla (feature store → kurallar → politika) kronolojik replay ile üretildi; profil öğrenmesi canlı sistemle aynı `should_learn` kuralını kullanır (parite testi: `test_a3_*`). Replay'de champion model yoktur; modelin ALLOW sınırını değiştirdiği olaylar kalan kaymadır.

## Test metrikleri
| Bileşen | PR-AUC | ROC-AUC | Recall @ %1 FPR | Kesinlik @ %1 bütçe | Maliyet ağırlıklı recall @ %1 bütçe |
|---|---|---|---|---|---|
| Kural motoru | 0.599 | 0.879 | 0.656 | 0.863 | 0.480 |
| LightGBM | 0.843 | 0.975 | 0.855 | 0.926 | 0.509 |
| Anomali (IForest+ECOD) | 0.560 | 0.922 | 0.597 | 0.863 | 0.388 |
| **Hibrit (stacker)** | 0.836 | 0.973 | 0.851 | 0.916 | 0.507 |

Maliyet ağırlıklı recall: işlemlerin en riskli %1'i alert olduğunda yakalanan fraud **tutarının** toplam fraud tutarına oranı.

## Kalibrasyon (test dönemi)
| Çıktı | Brier | ECE (10 kutu) | Ort. tahmin | Gözlenen oran |
|---|---|---|---|---|
| LightGBM (ham çıktı) | 0.00869 | 0.00838 | 0.0149 | 0.0232 |
| Hibrit (stacker) | 0.00662 | 0.00378 | 0.0207 | 0.0232 |

Kalibrasyon doğrulama dağılımında yapılır; test döneminde taban oran kayarsa ECE büyür. Etiket gürültüsü (kaçan fraud) gözlenen oranı gerçek oranın altında tutar.

## Tipoloji bazında politika sonuçları (test, harici sinyaller hariç)
| Tipoloji | Adet | ALLOW | STEP_UP | HOLD | BLOCK |
|---|---|---|---|---|---|
| app | 17 | 0 | 0 | 2 | 15 |
| ato | 18 | 2 | 2 | 6 | 8 |
| card_testing | 101 | 0 | 1 | 11 | 89 |
| mule | 86 | 49 | 9 | 13 | 15 |
| normal | 9278 | 9275 | 2 | 1 | 0 |
| structuring | 14 | 0 | 0 | 1 | 13 |

Eşikler: {'step_up': 0.35, 'hold': 0.6, 'block': 0.85}.

## En etkili feature'lar (LightGBM gain)
| Feature | Açıklama | Pay |
|---|---|---|
| `amount_ratio` | Tutarın müşterinin adaptif ortalamasına oranı | 0.168 |
| `time_since_last_s` | Bir önceki işlemden bu yana geçen süre (sn) | 0.108 |
| `payee_relation_age_d` | Alıcıyla ilk işlemden bu yana gün (-1: yeni) | 0.089 |
| `amount_zscore` | Tutarın müşterinin EWMA dağılımına göre z-skoru | 0.070 |
| `paste_used` | Tutar/IBAN yapıştırıldı | 0.056 |
| `payee_age_d` | Alıcının sistemde ilk görülmesinden bu yana gün (-1: yeni) | 0.055 |
| `amount_try` | İşlem tutarı (TRY'ye normalize) | 0.055 |
| `hour` | İşlem saati (0-23) | 0.048 |
| `is_new_device` | Cihaz müşteri için yeni (cihaz bilgisi yoksa 0) | 0.047 |
| `login_to_transfer_s` | Oturum açmadan transfere kadar geçen süre (sn, -1 yok) | 0.044 |
| `device_customers_7d` | Cihazı 7 günde kullanan farklı müşteri sayısı | 0.038 |
| `night_ratio_7d` | Son 7 gündeki gece işlemlerinin oranı | 0.033 |

## Açıklanabilirlik
Her kararda LightGBM `pred_contrib` (TreeSHAP; `shap.TreeExplainer` ile birebir aynı, testli) katkıları feature grubuna göre toplanıp `ML_*` reason code'larına eşlenir; kural isabetleri Türkçe şablonlu reason code üretir.

## Stacker
Girdi: logit(kural), logit(ML), logit(anomali); katsayılar [0.100977, 1.147558, 0.0], kesişim 1.894. Doğrulama bölmesinde negatif olmayan lojistik regresyon (çok değişkenli Platt): tek kısıt katsayı ≥ 0 (monotonluk — hiçbir bileşen riski düşüremez). Eski taban (≥ 0.05, kural ≥ 0.3) kaldırıldı; katsayısı 0 olan bileşen kullanılmıyor demektir. Anomali girdisi: dropped (validation ablation).

Ablasyon (validation first half -> fit, second half -> score (time order); satır [4757, 4757], pozitif [51, 75]):

| Varyant | Log-loss | Brier | PR-AUC | Katsayılar |
|---|---|---|---|---|
| `floored_legacy` | 0.02360 | 0.00486 | 0.822 | [0.3, 1.3591, 0.05] |
| `unconstrained` | 0.02139 | 0.00482 | 0.837 | [0.0765, 1.2737, -0.191] |
| `nonneg` | 0.02174 | 0.00485 | 0.831 | [0.0621, 1.1829, 0.0] |
| `nonneg_no_anomaly` | 0.02174 | 0.00485 | 0.831 | [0.0621, 1.1828, 0.0] |

## Sınırlamalar ve riskler
- Sentetik veriyle eğitildi; gerçek dağılımlarda yeniden eğitim ve kalibrasyon şart.
- APP dolandırıcılığında cihaz/konum tanıdık olduğundan model sosyal mühendislik sinyallerine (görüşme, uzaktan erişim, metin) dayanır; bu sinyaller yoksa kaçırma olasılığı artar — graf ve CoP sinyalleri politika katmanında bunu telafi eder.
- Yaşlı/kırılgan müşteri bayrağı riski artırır; amaç koruyucu friction'dır (uyarı, bekletme), ret değil. Adillik izlemesi için karar oranları segment bazında izlenmeli.
- Drift: skor ve en etkili 10 feature için PSI referans dağılımları metadata'dadır.

Eğitim süresi: 39.14 sn · ağaç sayısı: 100.

## Halka açık veride doğrulama

Model yalnızca sentetik veriyle eğitildi. Halka açık verilerdeki (PaySim, Elliptic, ULB) sonuçlar ve yöntem [`docs/VALIDATION_REPORT.md`](VALIDATION_REPORT.md) dosyasında. Sentetik modelin PaySim replay'inde PR-AUC v5 için 0,0119, v6 için 0,0122 (`artifacts/validation/champion_selection.json`). Yani sentetik veriyle eğitilen model PaySim'e taşınmıyor. PaySim'in kendi verisiyle eğitilen hattın sonuçları doğrulama raporunda.

## Yönetişim

- **Seçim kuralı (yalnız doğrulama dönemi):** %1 alarm bütçesinde kaçan fraud tutarı payı en düşük olan aday seçilir; eşitlikte doğrulama PR-AUC'si belirler. Doğrulama dönemindeki değerler: v5 0,1449, v6 0,1982. Test ve PaySim sonuçları karardan sonra bir kez raporlanır, seçimde kullanılmaz. Test dönemi: v5 hibrit PR-AUC 0,8355, Brier 0,00662, ECE 0,00378; v6 0,8401 / 0,00626 / 0,00375. Terfi dört göz ilkesiyle yapıldı: talep `admin`, onay `kidemli_analist`; kendi kendini onaylama 403 döner. Kaynaklar: `scripts/champion_selection.py`, `artifacts/validation/champion_selection.json`.
- **Champion / challenger:** Challenger her işlemde gölge olarak skorlanır, karara etki etmez. `GET /api/models/compare` çevrimdışı ve çevrimiçi karşılaştırma yapar.
- **Terfi:** Admin talebi ve farklı bir kıdemli kullanıcının onayı gerekir (maker-checker). Sonra canlı motor yeniden yüklenir.
- **Kalibrasyon:** Yalnız stacker çıktısı doğrulama diliminde (negatif olmayan Platt/lojistik) kalibre edilir; LightGBM ham çıktısına ayrı bir kalibratör uygulanmaz. Politika eşikleri (0,35 / 0,60 / 0,85) elle seçildi, veriyle optimize edilmedi. Stacker kural tabanını kaldırınca bazı açık vakalarda risk skoru BLOCK eşiğinin altına iner; bu olaylar kural aksiyon tabanıyla (`RULE_FLOOR`) BLOCK'a taşınır.
- **Drift:** Skor ve en etkili 10 feature için PSI izlenir (0,10 izle / 0,25 alarm): `fraud_drift_psi`, `GET /api/models/drift`. Prod dışında (`drift_reference=auto`) PSI referansı modelin eğitim dağılımından değil, açılıştaki demo popülasyonundan kurulur. Böylece demo açılışında yanlış alarm oluşmaz. Prod'da modelin referansı kullanılır.
- **Geri besleme:** Profil öğrenmesi tek bir kurala bağlıdır (`app/features/learning.py::should_learn`):
  - Doğrulanmış fraud hiç öğrenilmez.
  - Başarılı step-up (`POST /api/transactions/{id}/step-up-result`) ve analistin "temiz" etiketi öğrenilir.
  - Geri bildirim yoksa yalnız ALLOW öğrenilir.

  Sentetik replay'de step-up sonucu gerçeğe bakan bir kâhin değildir (`StepUpOutcomeModel`): Dolandırıcının kontrolündeki işlemlerin %30'u geçer. APP mağdurları işlemi kendileri onayladığı için %90 oranında geçer; mule ve structuring işlemleri hesap sahibi yaptığı için her zaman geçer. Gerçek müşterilerin %5'i takılır. Analist kararları `labels` tablosuna yazılır. `scripts/train_models.py --incremental` bunlardan yeni bir versiyon üretir. `GET /api/models/active-learning` belirsiz (0,4–0,6) işlemleri önerir.
