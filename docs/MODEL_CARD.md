# Model kartı — `fraud_gbm_v3` (champion)

> Bu kart `models/fraud_gbm_v3/model_card.md` dosyasının kopyasıdır ve eğitim betiğinin ürettiği sayıları içerir. Champion `fraud_gbm_v3`, challenger `fraud_gbm_v4` (`models/registry.json`). İkisi de parmak izlerinden arındırılmış sentetik üreticiyle eğitildi. Önceki `fraud_gbm_v1`/`v2` (PR-AUC 0,971/0,967) arşivlendi: o değerler eski üreticinin etiketi cihaz kimliğine sızdırmasından geliyordu (`docs/VALIDATION_REPORT.md` §1).

## Amaç ve kapsam
Gerçek zamanlı giden transferlerde (FAST/EFT/havale/kart) dolandırıcılık olasılığı üretir. Karar **vermez**: çıktısı kural skoru ve anomali skoru ile birlikte lojistik stacker'a, oradan politika katmanına (ALLOW / STEP_UP / HOLD / BLOCK) gider. Yaptırım taraması, hesap durumu ve kural aksiyon tabanları deterministik override'lardır.

## Veri
- Sentetik, seed'li Türk bankacılığı verisi (seed=42); 500 müşteri, 62869 işlem, fraud oranı 0.02564.
- Tipolojiler: app: 74, ato: 117, card_testing: 278, mule: 419, normal: 61879, sanctions: 10, structuring: 92 (yaptırım isabetleri ML eğitiminden hariç).
- Zaman bazlı bölme 70/15/15 — eğitim 44001 (1064 fraud), doğrulama 9429 (284), test 9429 (254); test dönemi 2026-07-21T19:34:48 → 2026-07-31T03:34:40.
- Feature'lar online skorlayıcıyla **aynı** motorla (feature store → kurallar → politika) kronolojik replay ile üretildi; profil öğrenmesi canlı sistemle aynı `should_learn` kuralını kullanır (parite testi: `test_a3_*`). Replay'de champion model yoktur; modelin ALLOW sınırını değiştirdiği olaylar kalan kaymadır.

## Test metrikleri
| Bileşen | PR-AUC | ROC-AUC | Recall @ %1 FPR | Kesinlik @ %1 bütçe | Maliyet ağırlıklı recall @ %1 bütçe |
|---|---|---|---|---|---|
| Kural motoru | 0.314 | 0.722 | 0.319 | 0.660 | 0.600 |
| LightGBM | 0.570 | 0.784 | 0.547 | 0.989 | 0.608 |
| Anomali (IForest+ECOD) | 0.267 | 0.772 | 0.260 | 0.585 | 0.561 |
| **Hibrit (stacker)** | 0.558 | 0.786 | 0.535 | 0.989 | 0.676 |

Maliyet ağırlıklı recall: işlemlerin en riskli %1'i alert olduğunda yakalanan fraud **tutarının** toplam fraud tutarına oranı.

## Tipoloji bazında politika sonuçları (test, harici sinyaller hariç)
| Tipoloji | Adet | ALLOW | STEP_UP | HOLD | BLOCK |
|---|---|---|---|---|---|
| app | 6 | 0 | 0 | 0 | 6 |
| ato | 24 | 0 | 0 | 0 | 24 |
| card_testing | 32 | 0 | 0 | 0 | 32 |
| mule | 65 | 17 | 3 | 10 | 35 |
| normal | 9280 | 9239 | 24 | 13 | 4 |
| structuring | 22 | 0 | 0 | 0 | 22 |

Eşikler: {'step_up': 0.35, 'hold': 0.6, 'block': 0.85}.

## En etkili feature'lar (LightGBM gain)
| Feature | Açıklama | Pay |
|---|---|---|
| `amount_ratio` | Tutarın müşterinin adaptif ortalamasına oranı | 0.288 |
| `payee_age_d` | Alıcının sistemde ilk görülmesinden bu yana gün (-1: yeni) | 0.079 |
| `payee_relation_age_d` | Alıcıyla ilk işlemden bu yana gün (-1: yeni) | 0.066 |
| `amount_try` | İşlem tutarı (TRY'ye normalize) | 0.058 |
| `time_since_last_s` | Bir önceki işlemden bu yana geçen süre (sn) | 0.053 |
| `is_new_payee` | Bu alıcıya ilk kez para gönderiliyor | 0.052 |
| `amount_zscore` | Tutarın müşterinin EWMA dağılımına göre z-skoru | 0.043 |
| `session_duration_s` | Oturum süresi (sn, -1 yok) | 0.037 |
| `device_age_d` | Cihazın sistemde ilk görülmesinden bu yana gün (-1: cihaz yok) | 0.034 |
| `login_to_transfer_s` | Oturum açmadan transfere kadar geçen süre (sn, -1 yok) | 0.034 |
| `sum_7d` | Son 7d içindeki toplam tutar (TRY) | 0.024 |
| `paste_used` | Tutar/IBAN yapıştırıldı | 0.022 |

## Açıklanabilirlik
Her kararda LightGBM `pred_contrib` (TreeSHAP; `shap.TreeExplainer` ile birebir aynı, testli) katkıları feature grubuna göre toplanıp `ML_*` reason code'larına eşlenir; kural isabetleri Türkçe şablonlu reason code üretir.

## Stacker
Girdi: logit(kural), logit(ML), logit(anomali); katsayılar [0.3, 1.3762011140716468, 0.05], kesişim 2.763. Katsayılar ≥ 0.05 ile tabanlanır (monotonluk: hiçbir bileşen riski düşüremez).

## Sınırlamalar ve riskler
- Sentetik veriyle eğitildi; gerçek dağılımlarda yeniden eğitim ve kalibrasyon şart.
- APP dolandırıcılığında cihaz/konum tanıdık olduğundan model sosyal mühendislik sinyallerine (görüşme, uzaktan erişim, metin) dayanır; bu sinyaller yoksa kaçırma olasılığı artar — graf ve CoP sinyalleri politika katmanında bunu telafi eder.
- Yaşlı/kırılgan müşteri bayrağı riski artırır; amaç koruyucu friction'dır (uyarı, bekletme), ret değil. Adillik izlemesi için karar oranları segment bazında izlenmeli.
- Drift: skor ve en etkili 10 feature için PSI referans dağılımları metadata'dadır.

Eğitim süresi: 21.05 sn · ağaç sayısı: 91.

## Halka açık veride doğrulama

Model yalnızca sentetik veriyle eğitildi. Halka açık veri üzerindeki sonuçlar (PaySim, Elliptic, ULB) ve yöntemi: [`docs/VALIDATION_REPORT.md`](VALIDATION_REPORT.md). Sentetik modelin PaySim replay'inde (`artifacts/validation/champion_selection.json`) PR-AUC v3 için 0,0356, v4 için 0,0166; yani sentetik veriyle eğitilen model PaySim'e taşınmıyor. PaySim üzerinde kendi verisiyle eğitilen hattın sonuçları doğrulama raporundadır.

## Yönetişim

- **Seçim kuralı:** %1 alarm bütçesinde sentetik holdout'ta kaçan fraud tutarı payı en düşük aday (v3 0,324, v4 0,3444); eşitlikte PaySim replay toplam maliyeti. Terfi dört göz ilkesiyle yapıldı (talep `admin`, onay `kidemli_analist`; kendi kendini onaylama 403). Kaynak: `scripts/champion_selection.py`, `artifacts/validation/champion_selection.json`.
- **Champion / challenger:** challenger her işlemde gölge skorlanır, karara etki etmez; `GET /api/models/compare` çevrimdışı ve çevrimiçi karşılaştırma yapar.
- **Terfi:** admin talebi + farklı kıdemli kullanıcının onayı (maker-checker) → canlı motor yeniden yüklenir.
- **Drift:** skor ve en etkili 10 feature için PSI (0,10 izle / 0,25 alarm) — `fraud_drift_psi`, `GET /api/models/drift`. Prod dışında (`drift_reference=auto`) PSI referansı modelin eğitim dağılımından değil, açılışta demo popülasyonundan kurulur; böylece demo açılışında yanlış alarm oluşmaz. Prod'da model referansı kullanılır.
- **Geri besleme:** profil öğrenmesi tek kurala bağlı (`app/features/learning.py::should_learn`): doğrulanmış fraud hiç öğrenilmez; başarılı step-up (`POST /api/transactions/{id}/step-up-result`) veya analistin "temiz" etiketi öğrenilir; geri bildirim yoksa yalnız ALLOW öğrenilir. Analist kararları `labels` tablosuna yazılır; `scripts/train_models.py --incremental` yeni versiyon üretir; `GET /api/models/active-learning` belirsiz (0,4–0,6) işlemleri önerir.
