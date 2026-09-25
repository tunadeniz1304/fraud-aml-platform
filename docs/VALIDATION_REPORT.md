# Doğrulama raporu: halka açık veri ve sentetik veri

Bu rapordaki bütün sayılar `artifacts/validation/` altındaki JSON dosyalarından alınmıştır
(üretim zamanı 2026-09-25). Veri kaynakları, lisanslar ve sızıntı kararları için
[`docs/DATA.md`](DATA.md), yöntem için `app/validation/*.py` modül açıklamaları esas alınır.

| Kaynak dosya | İçerik |
|---|---|
| `artifacts/validation/paysim/metrics.json` | PaySim, %10 alıcı-hash örneği, 743 adımın tamamı |
| `artifacts/validation/paysim_fixture/metrics.json` | Repodaki 19.999 satırlık PaySim örneği (indirme gerektirmez) |
| `artifacts/validation/synthetic/metrics.json` | Parmak izi temizlenmiş sentetik üretici, seed 42 |
| `artifacts/validation/elliptic/metrics.json`, `elliptic_fixture/metrics.json` | Elliptic Bitcoin grafı (graf modülü) |
| `artifacts/validation/ulb/metrics.json` | ULB kredi kartı (OpenML 1597) |
| `artifacts/validation/champion_selection.json` | Champion / challenger seçimi |

---

## 1. Özet

- **Sinyalin neredeyse tamamını GBM taşıyor.** PaySim'de `rules` katmanının PR-AUC değeri 0.1059,
  `rules+gbm` katmanınınki 0.4941. Aradaki fark +0.3882 (95 % GA [0.3358, 0.4391]). Sentetik
  veride fark +0.245 (GA [0.2093, 0.2794]).
- **Anomali katmanının etkisi küçük ve veri setine göre değişiyor.** PaySim'de PR-AUC'yi +0.0027
  artırıyor (GA [0.0002, 0.0057]). Aralık 0'ı dışarıda bıraktığı için sonuç "katkı var", ancak
  etki çok küçük. Sentetik veride PR-AUC −0.0162 değişiyor (GA [−0.0226, −0.0099]), sonuç **"zarar"**.
- **Graf / burst sinyallerinin (`+graph`) katkısı bulunamadı.** PaySim'de Δ = 0.0 (GA [0.0, 0.0]).
  Sentetik veride Δ = +0.0054, ancak GA [−0.0014, 0.0123] 0'ı içeriyor. İki veri setinde de
  sonuç "katkı yok".
- **Elliptic'te yapısal graf özellikleri, veriyle gelen özelliklerin üstüne bir şey eklemiyor.**
  Tek başına `graph` (4 özellik) illicit F1 değeri 0.1158. `all` (165 özellik) için F1 0.8149,
  `all+graph` için 0.7903.
- **Sentetik veride eski 0.971 PR-AUC değeri bir sızıntıdan geliyordu.** Eski üretici etiketi
  cihaz kimliğine (`DEV-ATO-*`) yazıyordu. Üretici temizlendikten sonra aynı hat sentetik veride
  0.6121 (`full`) PR-AUC veriyor. PaySim'de değer 0.4968.
- **Uygulanan eylemler (PaySim `full`).** Test dönemindeki 219 fraud işleminin 147'si ALLOW
  aldı. 1 % alarm bütçesinde recall 0.5342, precision 0.4164. Yakalanan tutar, toplam fraud
  tutarının 0.9102'si (tutar ağırlıklı recall).
- **ML hattı gerçek kart verisinde çalışıyor (ULB).** GBM'in PR-AUC değeri 0.7969. Anomali
  eklenen hibrit skor daha düşük: 0.7704.

---

## 2. Yöntem

### 2.1 Gölge replay

`app/validation/replay.py`: Etiketli olay akışı **zaman sırasıyla**, canlı sistemin kullandığı
bileşenlerden geçer. Bu bileşenler şunlardır: bellek içi feature store, kural DSL'i, dış sinyaller
(burst, entity graph) ve politika katmanı. Replay hiçbir hesaba dokunmaz ("gölge replay"). Sıra
şöyledir:

```
feature store → rules → GBM → anomaly (IForest + ECOD) → graph/burst sinyalleri → policy
```

- Özellikler, canlı motorun kullandığı fonksiyonlarla hesaplanır (online/offline eşitliği).
  Profil öğrenme `app.features.learning.should_learn` ile yapılır. Step-up sonucu etiketten
  simüle edilir; eğitim backfill'inde de aynısı yapılır. Backfill ile canlı replay'in aynı
  özellikleri ürettiği şu test ile kontrol edilir:
  `tests/test_audit_v2.py::test_a3_backfill_and_live_replay_produce_identical_features`.
- Replay sırasında analist fraud etiketleri grafa **geri beslenmez**. Beslenseydi test
  etiketleri sızardı.

### 2.2 Zaman bölmesi

- Zaman aralığının ilk 70 %'i eğitim, son 30 %'u testtir. Eğitim döneminin son 15 %'i
  doğrulama (validation) için ayrılır. Bütün modeller yalnızca eğitim döneminde eğitilir.
- PaySim'de eğitim `step` ≈ 520.4'e kadar sürer (`train_until_step`), test bu adımdan sonra başlar.

### 2.3 Katmanlar ve ablation

`app/validation/evaluate.py`: Her katman bir öncekine tek bir bileşen ekler.

1. `rules`: Kural DSL skoru (noisy-OR).
2. `rules+gbm`: Kural skoru ile LightGBM olasılığının lojistik stack'i.
3. `+anomaly`: Üretimdeki stacker (kural, GBM, IForest+ECOD).
4. `+graph`: Politikanın burst ve entity-graph sinyal skorlarıyla yaptığı noisy-OR birleşimi
   (yapılandırılmış ağırlıklarla).
5. `full`: Politikanın tamamı. Kural eylem tabanları, tipoloji tavanları ve eşikler uygulanır,
   sonuçta risk skoru ve ALLOW / STEP_UP / HOLD / BLOCK kararı çıkar.

Her katmanın katkısı, önceki katmana göre PR-AUC farkıdır. Bu fark **eşleştirilmiş bootstrap**
ile hesaplanır. Karar kuralı şöyledir:
- 95 % güven aralığı 0'ı içeriyorsa sonuç **"katkı yok"**.
- Aralık tamamen 0'ın üstündeyse **"katkı var"**.
- Aralık tamamen 0'ın altındaysa **"zarar"**.

### 2.4 Metrikler

- **PR-AUC ve ROC-AUC.** 95 % bootstrap güven aralığıyla verilir (`bootstrap_rounds` = 200).
- **recall@1%FPR.** Yanlış pozitif oranı 1 % iken yakalanan fraud payı.
- **Alarm bütçesi 0.5 % ve 1 %.** Test işlemlerinin en riskli 0.5 % / 1 %'i alarm sayılır.
  Her bütçe için recall, precision ve tutar ağırlıklı recall (`cost_weighted_recall`) raporlanır.
- **Maliyet.** Kaçan fraud tutarına, yanlış alarm sayısı × `review_cost_try` eklenir
  (`review_cost_try` = 50.0 TRY). Karşılaştırma değeri `baseline_cost_no_system`'dır, yani
  hiç sistem olmasaydı kaybedilecek fraud tutarının tamamı.
- **Eylem dağılımı.** `full` politikasının test dönemindeki ALLOW / STEP_UP / HOLD / BLOCK
  sayıları. Bütün işlemler ve yalnız fraud işlemleri için ayrı verilir.
- **Politika eşikleri** (JSON `thresholds`): step_up 0.35, hold 0.6, block 0.85.

---

## 3. PaySim sonuçları

### 3.1 Alt küme

| Alan | Değer |
|---|---|
| Kaynak | `PS_20174392719_1491204439457_log.csv` (tam dosya, 6.362.620 satır; bkz. DATA.md) |
| `sample_frac` | 0.1 |
| Örnekleme birimi | Alıcı (`nameDest`) bazlı hash örneklemesi. Bir alıcının bütün işlemleri ya birlikte alınır ya birlikte dışarıda kalır, bu yüzden alıcı tarafındaki özellikler bozulmaz |
| Adımlar | 1–743 (tamamı) |
| `train_until_step` | 520.4 |
| Satır (toplam / eğitim / doğrulama / test) | 637.559 / 518.020 / 91.415 / 28.124 |
| Test fraud | 219 (oran 0.00779) |
| Model | 48 özellik, 72 ağaç |
| Replay süresi | 1377.9 sn |

### 3.2 Katman sonuçları

| Katman | PR-AUC [95 % GA] | ROC-AUC [95 % GA] | recall@1%FPR | Recall @0.5 % | Precision @0.5 % | Recall @1 % | Precision @1 % |
|---|---|---|---|---|---|---|---|
| `rules` | 0.1059 [0.0726, 0.1437] | 0.7368 [0.6993, 0.7696] | 0.0594 | 0.0822 | 0.1277 | 0.1735 | 0.1352 |
| `gbm` | 0.4661 [0.3993, 0.5367] | 0.9105 [0.8788, 0.9338] | 0.5936 | 0.3699 | 0.5745 | 0.5297 | 0.4128 |
| `rules+gbm` | 0.4941 [0.4287, 0.5541] | 0.8714 [0.8349, 0.9012] | 0.6164 | 0.3881 | 0.6028 | 0.5342 | 0.4164 |
| `+anomaly` | 0.4968 [0.4317, 0.5552] | 0.8803 [0.8409, 0.9083] | 0.6073 | 0.3744 | 0.5816 | 0.5342 | 0.4164 |
| `+graph` | 0.4968 [0.4317, 0.5552] | 0.8803 [0.8409, 0.9083] | 0.6073 | 0.3744 | 0.5816 | 0.5342 | 0.4164 |
| `full` | 0.4968 [0.4317, 0.5552] | 0.8803 [0.8409, 0.9083] | 0.6073 | 0.3744 | 0.5816 | 0.5342 | 0.4164 |

Tek başına `gbm` satırının ROC-AUC değeri (0.9105), stack'lenmiş katmanlarınkinden (0.8714–0.8803)
yüksek. PR-AUC ise stack'lenmiş katmanlarda daha yüksek. Stacker yüksek skor bölgesini
iyileştiriyor, düşük skor bölgesindeki sıralamayı ise bozuyor.

![PaySim PR eğrileri](img/validation/paysim_pr.png)

### 3.3 Ablation (eşleştirilmiş bootstrap, ΔPR-AUC önceki katmana göre)

| Katman | ΔPR-AUC | 95 % GA | Karar |
|---|---|---|---|
| `rules+gbm` | +0.3882 | [0.3358, 0.4391] | katkı var |
| `+anomaly` | +0.0027 | [0.0002, 0.0057] | katkı var (çok küçük) |
| `+graph` | 0.0 | [0.0, 0.0] | katkı yok |
| `full` | 0.0 | [−0.0, 0.0] | katkı yok |

`+graph` katmanının PaySim'de hiç etkisi yok. Graf ve burst sinyali skorları test döneminde
sıralamayı değiştirmiyor.

### 3.4 Eylem dağılımı (`full`, test dönemi)

| Küme | ALLOW | STEP_UP | HOLD | BLOCK | Toplam |
|---|---|---|---|---|---|
| Tüm işlemler | 28.034 | 36 | 29 | 25 | 28.124 |
| Fraud işlemleri | 147 | 21 | 26 | 25 | 219 |

- BLOCK kararı verilen 25 işlemin 25'i fraud.
- HOLD kararı verilen 29 işlemin 26'sı fraud.
- Fraud işlemlerinin 147'si (219'un yaklaşık üçte ikisi) ALLOW ile geçiyor. Politika eşikleri
  sentetik veriye göre ayarlandı. PaySim'deki skor dağılımında bu eşiklerin üstüne az işlem çıkıyor.

### 3.5 Maliyet (`full`, 1 % alarm bütçesi)

| Kalem | Değer |
|---|---|
| Alarm | 281 |
| Yanlış alarm | 164 |
| Toplam fraud tutarı | 286,805,594.67 |
| Yakalanan fraud tutarı | 261,059,323.93 |
| Kaçan fraud tutarı | 25,746,270.74 |
| İnceleme maliyeti (164 × 50.0) | 8,200.0 |
| **Toplam maliyet** | **25,754,470.74** |
| Sistem olmadan maliyet | 286,805,594.67 |
| Tutar ağırlıklı recall | 0.9102 |

Karşılaştırma için aynı bütçede `rules` katmanının toplam maliyeti 123,268,182.39, `gbm`
katmanınınki 30,591,151.04. Tutarlar PaySim birimindedir; `paysim_try_per_unit` varsayılanı 1.0.

### 3.6 En önemli özellikler (GBM gain payı)

| Özellik | Pay |
|---|---|
| `hour` | 0.23185 |
| `amount_zscore` | 0.22187 |
| `payee_age_d` | 0.16386 |
| `is_cash_channel` | 0.13448 |
| `amount_try` | 0.09319 |
| `amount_log` | 0.06457 |
| `payee_fan_in_24h` | 0.0558 |
| `hour_unusual` | 0.01405 |
| `amount_ratio` | 0.01223 |
| `is_night` | 0.00803 |
| `near_threshold` | 4e-05 |
| `near_threshold_cnt_24h` | 4e-05 |

Sinyali saat, tutar, kanal ve alıcı tarafındaki özellikler (`payee_age_d`, `payee_fan_in_24h`)
taşıyor. Müşteri tarafındaki hız (velocity) ve profil özellikleri listede yok.

### 3.7 PaySim'in sınırlamaları

Ayrıntı için [DATA.md §3](DATA.md#3-paysim-eşlemesi-ve-sızıntı-kararları).

- **`nameOrig` neredeyse benzersiz.** 6,36 milyon satırda 6,35 milyon farklı değer var.
  Müşterilerin hemen hepsi tek işlem yapıyor. Bu yüzden müşteri bazlı velocity ve davranış
  profili özellikleri PaySim'de bilgi taşımıyor. Bilgi alıcı tarafında.
- **Cihaz, IP ve oturum bilgisi yok.** Bu alanlar uydurulmadı. Feature store bunları eksiklik
  göstergelerine çeviriyor (`device_missing=1`, oturum özellikleri için `-1`). Cihaz ve oturum
  tabanlı ATO sinyalleri PaySim'de test edilemiyor.
- **Bazı kolonlar bilerek kullanılmadı.** Bakiye kolonları (`oldbalanceOrg`, `newbalanceOrig`,
  `oldbalanceDest`, `newbalanceDest`) ve `isFlaggedFraud` özellik olarak kullanılmadı. Simülatör
  bakiyeleri etikete bağlı biçimde güncelliyor. `isFlaggedFraud` ise PaySim'in kendi kural çıktısı.
- **Müşteri KYC bilgisi yok.** Tutar önseli, eğitim dönemindeki popülasyon medyanı
  (`amount_prior_try` = 75490.85).

### 3.8 Offline fixture (`--source fixture`)

`tests/fixtures/paysim_sample.csv` dosyası 19.999 satırdır ve indirme gerektirmez. Bu çalıştırmada
bölünme şöyle: toplam 19.999, eğitim 16.194, doğrulama 2.857, test 948. Testte yalnızca 7 fraud
var, bu yüzden aralıklar çok geniş. Örneğin `full` PR-AUC 0.2869, GA [0.0143, 0.6249]. `full`
politikası testteki bütün işlemlere ALLOW veriyor. Fixture yalnızca hattın uçtan uca çalıştığını
ve sonucun yeniden üretilebildiğini kontrol etmek içindir. Performans kanıtı olarak
kullanılmamalıdır.

![PaySim fixture PR eğrileri](img/validation/paysim_fixture_pr.png)

---

## 4. Sentetik ile gerçek yan yana

Sentetik veri: seed 42, 500 müşteri, 62.869 işlem, 1.612 fraud (oran 0.02564), 4 halka.
Test kümesi 19.146 işlem, 550 fraud. PaySim verisi §3'teki gibidir.

| Katman | Sentetik PR-AUC | Sentetik ROC-AUC | PaySim PR-AUC | PaySim ROC-AUC |
|---|---|---|---|---|
| `rules` | 0.3779 | 0.7416 | 0.1059 | 0.7368 |
| `gbm` | 0.6253 | 0.8271 | 0.4661 | 0.9105 |
| `rules+gbm` | 0.6229 | 0.8263 | 0.4941 | 0.8714 |
| `+anomaly` | 0.6067 | 0.8236 | 0.4968 | 0.8803 |
| `+graph` | 0.6121 | 0.8213 | 0.4968 | 0.8803 |
| `full` | 0.6121 | 0.8213 | 0.4968 | 0.8803 |
| *Eski: `fraud_gbm_v1`, parmak izi olan üretici (registry: `archived`)* | *0.971* | *0.9925* | — | — |

Sentetik ablation: `rules+gbm` +0.245 [0.2093, 0.2794] katkı var. `+anomaly` −0.0162
[−0.0226, −0.0099] zarar. `+graph` +0.0054 [−0.0014, 0.0123] katkı yok. `full` 0.0 katkı yok.

![Sentetik PR eğrileri](img/validation/synthetic_pr.png)

**Eski 0.971 ile bugünkü 0.6121 arasındaki farkın nedenleri:**

1. **Parmak izi kaldırıldı.** Eski üretici, ATO işlemlerinde `DEV-ATO-*` biçiminde cihaz
   kimlikleri üretiyordu. Etiket böylece cihaz alanına yazılmış oluyordu. `fraud_gbm_v1`
   modelinde `is_new_device` ve `device_age_d` birlikte gain'in 54 %'ünü taşıyordu. Model
   dolandırıcılığı değil üreticinin kimlik biçimini öğreniyordu. Temizlenmiş üreticide en önemli
   özellikler tutar oranı (`amount_ratio` 0.18308), alıcı yaşı ve ilişki yaşı, zamanlama ve oturum
   özellikleri. Gain dağılımı daha düz: `device_age_d` 0.0484.
2. **Etiket gürültüsü var.** Yeni üretici etiketlerin 1 %'ini çeviriyor
   (`label_noise_rate` = 0.01). Bu, kaçırılmış chargeback ve friendly fraud durumlarını temsil
   ediyor. Böylece ulaşılabilecek en yüksek PR-AUC da aşağı çekiliyor.
3. **İki veri setinde farklı özellikler var.** Sentetik veride cihaz, oturum, müşteri geçmişi ve
   alıcı ilişkisi bulunuyor. PaySim'de bunların hiçbiri yok (§3.7). PaySim'de sinyal yalnızca
   saat, tutar, kanal ve alıcı tarafından geliyor. Sentetik veride kurallar daha güçlü
   (`rules` PR-AUC 0.3779, PaySim'de 0.1059). Bunun nedeni kural kataloğunun ve üreticinin aynı
   tipolojileri modellemesi.
4. **Temel oranlar farklı.** Sentetik testte fraud oranı 0.02873, PaySim testinde 0.00779.
   PR-AUC temel orana bağlı olduğu için iki setin PR-AUC değerleri doğrudan karşılaştırılamaz.

---

## 5. Elliptic (graf modülü)

`app/validation/elliptic.py`: Düğüm sınıflandırması yapılır (illicit / licit). "Unknown" düğümler
grafta kalır ama skorlanmaz.

- **Veri:** 203.769 düğüm, 234.355 kenar.
- **Bölme:** Weber et al. (2019)'daki standart zaman bölmesi kullanıldı. Zaman adımı **1–34
  eğitim, 35–49 test**. Eğitimde 29.894 düğüm var (3.462 illicit), testte 16.670 düğüm (1.083 illicit).
- **Model:** LightGBM. Farklı özellik kümeleriyle karşılaştırıldı.
- **Graf özellikleri (4 adet):** `pagerank`, `in_degree`, `out_degree`, `component_size`.
  Bunlar platformun mule/graf modülündeki yapısal özellikler.
- **Eşik:** Karar eşiği yalnızca eğitim verisinde, out-of-fold tahminlerle F1'i en yüksek
  yapacak şekilde seçildi. Test verisi eşik seçimine girmedi. Karşılaştırma için 0.5 eşiğindeki
  F1 de verildi.

| Özellik kümesi | Özellik sayısı | Illicit F1 | Precision | Recall | PR-AUC | Eşik | F1 @0.5 |
|---|---|---|---|---|---|---|---|
| `graph` | 4 | 0.1158 | 0.0845 | 0.1837 | 0.083 | 0.2875 | 0.0733 |
| `local` | 93 | 0.7567 | 0.7982 | 0.7193 | 0.7868 | 0.3358 | 0.774 |
| `local+graph` | 97 | 0.7649 | 0.8366 | 0.7045 | 0.7832 | 0.4307 | 0.7713 |
| `all` | 165 | 0.8149 | 0.926 | 0.7276 | 0.8036 | 0.4213 | 0.8188 |
| `all+graph` | 169 | 0.7903 | 0.8509 | 0.7378 | 0.8065 | 0.2093 | 0.8209 |

**Yorum.** Yapısal graf özellikleri tek başına zayıf: F1 0.1158, PR-AUC 0.083. Veriyle gelen
özelliklere eklendiklerinde net bir kazanç görülmüyor:
- `local+graph` ile `local` arasında F1 farkı +0.0082, PR-AUC farkı −0.0036.
- `all+graph` ile `all` arasında F1 farkı −0.0246, PR-AUC farkı +0.0029.

Bu fark aralıkları için bootstrap güven aralığı hesaplanmadı. Farklar küçük ve yönleri tutarsız.
Sonuç olarak graf özellikleri veriyle gelen özelliklerin üstüne bilgi eklemiyor.

**Neden fraud-proximity özelliği kullanılmadı.** Platformda bilinen illicit bir düğüme olan
hop mesafesini ölçen bir özellik var. Bu doğrulamada bilerek kullanılmadı. Elliptic'te kenarlar
zaman adımları arasında hiç geçmiyor. Bu yüzden test düğümleri (35–49) hiçbir eğitim etiketine
ulaşamıyor. Eğitimdeki her illicit düğüm ise kendi tohumu oluyor (mesafe 0). Sonuçta özellik
yalnızca eğitim etiketini kodluyor. Bu özellikle yapılan ilk çalıştırmada illicit F1 0.13–0.23'e
düştü ve sızıntı bu şekilde fark edildi.

### Literatür karşılaştırması

Kaynak: Weber et al. (2019), *Anti-Money Laundering in Bitcoin: Experimenting with Graph
Convolutional Networks for Financial Forensics*,
[arXiv:1908.02591](https://arxiv.org/abs/1908.02591). Değerler Table 1 ve Table 2'den alındı.
Hepsi illicit sınıfa ait ve aynı zaman bölmesiyle hesaplanmış. AF: tüm özellikler, NE: node embedding.

| Yöntem | Illicit F1 | Kaynak |
|---|---|---|
| Logistic Regression (AF) | 0.481 | Table 1 |
| Random Forest (AF), P 0.956 / R 0.670 | 0.788 | Table 1 |
| Random Forest (AF + NE) | 0.796 | Table 1 |
| MLP (AF) | 0.653 | Table 1 |
| GCN | 0.628 | Table 1 |
| Skip-GCN | 0.705 | Table 1 |
| EvolveGCN | 0.720 | Table 2 |
| **Bu çalışma: LightGBM `all`** | **0.8149** (P 0.926 / R 0.7276) | `elliptic/metrics.json` |

- Bu çalışmadaki LightGBM `all` sonucu, makaledeki Random Forest (AF) sonucuyla aynı
  büyüklükte. İkisi de veriyle gelen 165 özelliği kullanan ağaç tabanlı modeller.
- Bu karşılaştırma aynı veri ve aynı bölme üzerinde yapıldı. Ancak farklı bir kod ve eşik
  seçim yöntemi kullanıldı. Bu yüzden küçük farklar anlamlı kabul edilmemeli.
- **GraphSAGE / GNN çalıştırılmadı.** `torch_geometric` kurulu değil. GNN karşılaştırması
  isteğe bağlı bir adım olarak bırakıldı ve yapılmadı.

**Fixture.** `tests/fixtures/elliptic_sample` 510 düğümden oluşuyor. Testte yalnızca 10 illicit
düğüm var, bu yüzden sonuçlar performans göstergesi değil. Örneğin `all` F1 0.3077, `all+graph`
F1 0.5333. Bu çalıştırma yalnızca hattın çalıştığını kontrol eder.

---

## 6. ULB kredi kartı (ML hattı sağlamlık kontrolü)

`app/validation/ulb.py`, OpenML 1597.

- **Bölme:** Eğitim 169.461, doğrulama 29.904, test 85.442 satır.
- **Test fraud:** 108 (oran 0.00126).

| Skor | PR-AUC | ROC-AUC | recall@1%FPR | Brier | Recall @1 % bütçe | Precision @1 % bütçe |
|---|---|---|---|---|---|---|
| `gbm` | 0.7969 | 0.9826 | 0.8796 | 0.00044 | 0.8796 | 0.1112 |
| `anomaly` | 0.0436 | 0.9431 | 0.5185 | — | 0.5093 | 0.0644 |
| `hybrid` | 0.7704 | 0.9569 | 0.8241 | 0.000429 | 0.8148 | 0.103 |

**Sonuç.** Bu veride anomali skorunu stack'e eklemek (`hybrid`) PR-AUC, ROC-AUC ve
recall@1%FPR değerlerini düşürüyor. Brier skoru ise çok az iyileşiyor.

**Sınırlamalar:**
- Özellikler anonimleştirilmiş PCA bileşenleri (artı `Amount`). Kural motoru, feature store ve
  graf katmanı bu veriye uygulanamıyor. Yalnızca ML hattı test ediliyor: LightGBM, IsolationForest
  + ECOD ile quantile kalibrasyonu ve lojistik stacker (`rule` girdisi 0'a sabit).
- OpenML kopyasında `Time` kolonu yok. Kaynağın kronolojik satır sırası korunduğu için satır
  indeksi zaman yerine kullanıldı.

---

## 7. Champion / challenger

`scripts/champion_selection.py`: Parmak izi temizlenmiş sentetik veri (seed 42) yeniden üretilir
ve üretim eğitim koduyla iki aday eğitilir. Adaylar iki yerde karşılaştırılır:
- (a) Sentetik zaman bölmesi holdout'u.
- (b) PaySim gölge replay'i. Bu bir transfer kontrolüdür: modeller PaySim'de yeniden
  eğitilmeden, PaySim özellikleri üzerinde çalıştırılır.

| Aday | Parametreler | Sentetik PR-AUC | Sentetik ROC-AUC | Sentetik recall@1%FPR | Sentetik tutar ağırlıklı recall @1 % | Sentetik kaçan tutar payı @1 % | PaySim PR-AUC | PaySim ROC-AUC | PaySim toplam maliyet @1 % |
|---|---|---|---|---|---|---|---|---|---|
| `fraud_gbm_v3` | seed 42, 300 tur, 100 IForest ağacı | 0.558 | 0.7862 | 0.5354 | 0.676 | 0.324 | 0.0356 | 0.5828 | 206,144,043.88 |
| `fraud_gbm_v4` | seed 7, 500 tur, 200 IForest ağacı | 0.5561 | 0.7855 | 0.5315 | 0.6556 | 0.3444 | 0.0166 | 0.5979 | 248,448,601.97 |

- **Seçim kuralı** (JSON `decision.rule`): 1 % alarm bütçesinde, sentetik holdout'ta kaçan fraud
  tutarı payı en düşük olan aday seçilir. Eşitlik olursa PaySim replay toplam maliyeti belirler.
- **Kazanan:** `fraud_gbm_v3` (kaçan pay 0.324, `fraud_gbm_v4` için 0.3444).
  `fraud_gbm_v4` challenger olarak gölge skorlamada kalır.
- **Transfer sonucu:** Sentetik veride eğitilen adaylar PaySim'e taşınmıyor. PaySim PR-AUC
  0.0356 / 0.0166. Karşılaştırma için PaySim'de eğitilen GBM 0.4661 veriyor (§3). Sentetik veride
  eğitilmiş bir model gerçek bir banka akışına yeniden eğitilmeden konmamalı.
- **Dört göz kaydı** (maker-checker, gerçek API üzerinden):

| Alan | Değer |
|---|---|
| `approval_id` | 1 |
| `requested_by` | `admin` |
| `approved_by` | `kidemli_analist` |
| Talep edenin kendi onayı | HTTP 403 (reddedildi) |
| Sonuç | ONAYLANDI |

---

## 8. Sınırlamalar ve yeniden üretim

### Komutlar

```bash
python scripts/fetch_public_fraud_data.py                                # PaySim + Elliptic + ULB → data/external/
python scripts/validate_public_data.py --dataset paysim --sample-frac 0.1
python scripts/validate_public_data.py --dataset paysim --source fixture # indirmesiz, tests/fixtures/
python scripts/validate_public_data.py --dataset elliptic
python scripts/validate_public_data.py --dataset ulb
python scripts/validate_public_data.py --dataset synthetic
python scripts/champion_selection.py                                     # --no-promote: terfi etmeden karşılaştır
```

Çıktılar şunlardır:
- `artifacts/validation/<set>/metrics.json`
- `docs/img/validation/` altındaki PR eğrileri

`--reuse-replay` bayrağı replay'i `data/external/cache/` altına kaydeder. Kod değiştiğinde bu
önbellek silinmelidir.

### Sınırlamalar

- **Kaggle setleri çalıştırılmadı.** IEEE-CIS, BAF (NeurIPS 2022) ve IBM AML atlandı, çünkü
  `KAGGLE_USERNAME` ve `KAGGLE_KEY` ortamda tanımlı değil (DATA.md §1).
- **PaySim'in %10'u kullanıldı.** Tam 6,36 milyon satırlık replay yapılmadı. Örnekleme alıcı
  bazlı hash ile yapıldı; bu, alıcı tarafındaki özellikleri korur ama müşteri tarafındaki sınırı
  değiştirmez (§3.7).
- **Replay hızı.** Replay tek süreçte çalışır.
  - PaySim: 637.559 olay 1377.9 sn sürdü, ortalama yaklaşık 463 olay/sn.
  - Sentetik: 62.869 olay 60.7 sn sürdü.
  - Ortalamalar `replay_seconds` / `rows.total` ile hesaplandı.
- **Eşikler PaySim'e göre ayarlanmadı.** Politika eşikleri ve graf ağırlıkları sentetik veri ve
  yapılandırmadan geliyor. PaySim'de fraud işlemlerinin çoğunun ALLOW alması (§3.4) büyük ölçüde
  bundan kaynaklanıyor.
- **Katkı iddiası yapılmıyor.** Graf katmanının PaySim ve sentetik veride, anomali katmanının da
  sentetik veride ve ULB'de katkısı gösterilemedi. Bu katmanlar hatta duruyor, ancak bu
  sonuçlarla bir performans katkısı iddia edilmemeli.
- **Elliptic'teki farklar için güven aralığı yok.** Elliptic özellik kümeleri arasındaki farklar
  için bootstrap güven aralığı hesaplanmadı.
