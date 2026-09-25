# Veri kaynakları, lisanslar ve sızıntı kararları

Bu proje üç tür veriyle çalışır:

1. **Sentetik veri.** `app/synthetic/generator.py` ile seed'li üretilir. Demo ve eğitim için kullanılır.
2. **Halka açık fraud veri setleri.** Yalnızca doğrulama için kullanılır.
3. **Küçük test örnekleri (fixture'lar).** Repoda durur; testler internetsiz çalışır.

Ağa çıkan tek kod `scripts/fetch_public_fraud_data.py`'dir. Tam veri setleri
`data/external/` altına iner ve bu dizin `.gitignore`'dadır.

```bash
python scripts/fetch_public_fraud_data.py            # PaySim + Elliptic + ULB (~800 MB)
python scripts/build_fixtures.py                     # tests/fixtures/ örneklerini yeniden üretir
python scripts/validate_public_data.py --dataset paysim --sample-frac 0.1
```

İndirme akışlıdır ve yarıda kalırsa kaldığı yerden devam eder (HTTP `Range`). İlerleme
çubuğu gösterir. Her dosya bir checksum ile doğrulanır:
- PaySim'in MD5'i Zenodo API'sinden gelir.
- Diğer dosyaların SHA-256'sı `scripts/public_data_checksums.json` içinde sabitlenir. İlk doğrulanmış indirmede kaydedilir, sonraki her çalıştırmada karşılaştırılır.

## 1. Veri setleri

| Set | Kaynak | Lisans | Boyut | Kullanım |
|---|---|---|---|---|
| **PaySim** | Zenodo record [22761688](https://zenodo.org/records/22761688) (API: `https://zenodo.org/api/records/22761688`) | CC BY 4.0 (Zenodo metadata) | 493.534.783 B, 6.362.620 satır, 8.213 fraud | Birincil replay doğrulaması |
| **Elliptic** | PyG aynası `https://data.pyg.org/datasets/elliptic/` (`torch_geometric` tarafından kullanılır) | **CC BY-NC-ND 4.0** (Kaggle `ellipticco/elliptic-data-set` metadata'sı) | 203.769 düğüm, 234.355 kenar | Graf modülü doğrulaması |
| **ULB Credit Card** | OpenML [1597](https://www.openml.org/d/1597) (`sklearn.datasets.fetch_openml(data_id=1597)`) | OpenML lisans alanı: "Public" | 284.807 satır, 492 fraud | ML hattının sağlamlık kontrolü |
| IEEE-CIS, BAF (NeurIPS 2022), IBM AML | Kaggle | Kaggle koşulları | — | **Atlandı.** `KAGGLE_USERNAME` ve `KAGGLE_KEY` ortamda tanımlı değil. Betik yalnızca değişkenlerin var olup olmadığına bakar, değerlerini okumaz |

### Atıflar

- **PaySim:** E. A. Lopez-Rojas, A. Elmir, S. Axelsson. *PaySim: A financial mobile money simulator for fraud detection.* 28th European Modeling and Simulation Symposium (EMSS), 2016.
- **Elliptic:** M. Weber, G. Domeniconi, J. Chen, D. K. I. Weidele, C. Bellei, T. Robinson, C. E. Leiserson. *Anti-Money Laundering in Bitcoin: Experimenting with Graph Convolutional Networks for Financial Forensics.* KDD '19 Workshop on Anomaly Detection in Finance, 2019. [arXiv:1908.02591](https://arxiv.org/abs/1908.02591). Veri: Elliptic.
- **ULB:** A. Dal Pozzolo, O. Caelen, R. A. Johnson, G. Bontempi. *Calibrating Probability with Undersampling for Unbalanced Classification.* IEEE SSCI, 2015.

### Checksum'lar (2026-09-25 indirmesi)

| Dosya | SHA-256 |
|---|---|
| `paysim/PS_20174392719_1491204439457_log.csv` (MD5 `e92a5f7447f43712f1dca473d0b0fa85`, Zenodo ile eşleşti) | `16910f90577b0d981bf8ff289714510bb89bc71bff7d3f220f024e287e4eea6b` |
| `elliptic/elliptic_txs_features.csv.zip` | `d33d62159e64b5e889f1a7ea880227c612775b58d409598855e0c4400fa52b3e` |
| `elliptic/elliptic_txs_edgelist.csv.zip` | `a2f9f6b67a39da2d8cf87fe77b9db89571ba6d880e5dd5b5991dc45c80fa34ec` |
| `elliptic/elliptic_txs_classes.csv.zip` | `4ca957f0ceffd5dd164e255c7d5ad9ee69a6fa64ae1dd94d6f113e5ebf3b07ba` |
| `ulb/creditcard.csv` (OpenML'den dışa aktarılmış) | `5ac0db239c79b8234914ac04242b5a864cd34cd9c4e73526ed3808185285e335` |

## 2. Test örnekleri (`tests/fixtures/`)

| Örnek | İçerik | Boyut | Atıf |
|---|---|---|---|
| `paysim_sample.csv` | 19.999 satır, 26 fraud (%0,130; tam veride %0,129). (`type`, `isFraud`) katmanlı rastgele örnek, seed 20260925 | 1,5 MB | `paysim_sample.ATTRIBUTION.md` |
| `elliptic_sample/` | 510 düğüm (31 yasa dışı, 171 meşru, 308 bilinmeyen). Zaman adımları 10, 30 (eğitim) ve 42 (test) için yasa dışı bir düğümden başlayan genişlik öncelikli bağlı alt graflar | 1,7 MB | `elliptic_sample/ATTRIBUTION.md` |

Elliptic'in lisansı (CC BY-NC-ND 4.0) verinin bir **parçasını değiştirmeden** ve ticari
olmayan amaçla paylaşmaya izin verir, uyarlanmış (türev) materyali paylaşmaya izin vermez.
Bu yüzden örnek satırlar yuvarlanmadan ve yeniden kodlanmadan, **birebir** kopyalanır.
Proje kodu MIT lisanslıdır. Bu veri örnekleri kendi lisanslarına tabidir.

## 3. PaySim eşlemesi ve sızıntı kararları

`app/datasets/paysim_adapter.py`:

| PaySim | Platform | Not |
|---|---|---|
| `step` (saat, 1–743) | `ts = paysim_base_date + step saat` | Aynı saatteki satırların dosya sırası saniye ofsetiyle korunur |
| `type` | kanal + amaç | `PAYMENT→mobile/Ödeme`, `TRANSFER→web/Transfer`, `CASH_OUT→atm/Nakit çekim`, `CASH_IN→atm/Nakit yatırma`, `DEBIT→web/Borç ödeme` |
| `nameOrig` | `customer_id` | |
| `nameDest` | `beneficiary_id` | Grafta aynı kimlikli müşterinin hesabı sayılır (`C…` kimlikleri) |
| `amount` | `amount × paysim_try_per_unit` | Oran config'te durur (varsayılan 1,0). PaySim tutarlarının para birimi yok |
| cihaz, IP, oturum | **boş** | **Uydurulmaz.** Feature store `device_missing=1` ve oturum özellikleri için `-1` üretir, model bu eksiklikle çalışır |
| müşteri KYC | yalnız `customer_id` | Tutar önseli, eğitim dönemindeki **popülasyon medyanıdır**. Müşteri bazında değer uydurulmaz |

**Özellik olarak kullanılmayan kolonlar** (`EXCLUDED_COLUMNS`):

- `isFlaggedFraud`: PaySim'in kendi kural çıktısıdır (tek işlemde 200.000'den büyük transfer).
  Modele verilseydi başka bir sistemin kararı "öğrenilmiş" olurdu.
- `oldbalanceOrg`, `newbalanceOrig`, `oldbalanceDest`, `newbalanceDest`: Simülatör, fraud bir
  hesabı boşalttığında bakiyeleri etikete bağlı biçimde günceller. Örneğin fraud
  işlemlerinde `newbalanceOrig == 0` ve `amount == oldbalanceOrg` neredeyse her zaman
  doğrudur. Alıcı bakiyeleri de fraud satırlarında tutarsız (0) kalır. Bu kolonlarla
  kurulan bir model sınıflandırıcı değil, simülatörün iç mantığını çözen bir dedektör olur.
  Gerçek bir bankada karşı taraf bakiyesi zaten bilinmez.

**PaySim'in sınırlaması.** `nameOrig` 6,36 milyon satırda 6,35 milyon farklı değer alır.
Müşterilerin neredeyse hepsi tek işlem yapar, bu yüzden müşteri bazlı hız (velocity) ve
davranış profili özellikleri PaySim'de bilgi taşımaz. Alıcı tarafı (fan-in, alıcı yaşı)
bilgi taşır. Sonuçlar bu sınırlama altında okunmalıdır (`docs/VALIDATION_REPORT.md`).
