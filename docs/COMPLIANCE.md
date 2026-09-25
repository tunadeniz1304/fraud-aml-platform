# Uyum notları

Bu belge hukuki görüş değildir. Platform aşağıdaki gereksinimler dikkate alınarak tasarlandı. Bir denetimden geçmedi, bir kurum tarafından onaylanmadı. Her referans resmî bir kaynağa bağlanır; kaynaklara erişim tarihi **2026-09-25**. Resmî kaynağa bağlanamayan ifade "doğrulanmadı" olarak işaretlidir.

## Referanslar ve uygulayan kod

| # | Referans | Resmî kaynak | Durum |
|---|---|---|---|
| R1 | 5549 sayılı Suç Gelirlerinin Aklanmasının Önlenmesi Hakkında Kanun (RG 18.10.2006, sayı 26323) | https://www.mevzuat.gov.tr/mevzuatmetin/1.5.5549.pdf | doğrulandı |
| R2 | Suç Gelirlerinin Aklanmasının ve Terörün Finansmanının Önlenmesine Dair Tedbirler Hakkında Yönetmelik (RG 9.1.2008, sayı 26751) | https://www.mevzuat.gov.tr/MevzuatMetin/21.5.200713012.pdf | doğrulandı |
| R3 | Bankaların Bilgi Sistemleri ve Elektronik Bankacılık Hizmetleri Hakkında Yönetmelik, BDDK (RG 15.03.2020, sayı 31069) | https://www.resmigazete.gov.tr/eskiler/2020/03/20200315-10.htm | yayım doğrulandı; yürürlük tarihi ve madde eşlemesi doğrulanmadı |
| R4 | 6698 sayılı Kişisel Verilerin Korunması Kanunu (RG 7.4.2016, sayı 29677) | https://www.mevzuat.gov.tr/mevzuatmetin/1.5.6698.pdf | doğrulandı; madde eşlemesi yapılmadı |
| R5 | AB PSD3 önerisi COM(2023) 366 ve PSR önerisi COM(2023) 367 | https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52023PC0366 · https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52023PC0367 | öneri metni doğrulandı; **kabul edilmiş değil / resmî kabul doğrulanmadı** |
| R6 | Birleşik Krallık PSR, APP dolandırıcılığı geri ödeme zorunluluğu: PS23/3 ve PS24/7 | https://www.psr.org.uk/publications/policy-statements/ps233-fighting-authorised-push-payment-fraud-a-new-reimbursement-requirement/ · https://www.psr.org.uk/publications/policy-statements/ps247-faster-payments-app-scams-reimbursement-requirement-confirming-the-maximum-level-of-reimbursement/ | doğrulandı |
| R7 | FATF listeleri, Haziran 2026 genel kurulu | `data/jurisdictions/fatf_2026-06.json`; kara liste MAS yeniden yayımıyla: https://www.mas.gov.sg/publications/fatf-statement/2026/june-2026 | kara liste doğrulandı (ikincil resmî kaynak); gri liste **doğrulanmadı** |

## MASAK — şüpheli işlem bildirimi (R1, R2)

- **Yükümlülük (R1 md. 4/1).** Şüpheli işlemlerin MASAK'a bildirilmesi. Platform bildirimi kendisi göndermez; analist için ŞİB taslağı hazırlar ve süreyi izler.
  - Kod: `app/copilot/templates.py` (ŞİB taslağı, `SibDraft` şeması), `app/cases/service.py` (vaka ve onay akışı).
- **Şüpheli işlem tanımı (R2 md. 27).** Tipoloji eşlemesi (AML parçalama/katmanlama, mule, yaptırım) kural ve sinyal katmanında yapılır; bu eşleme kurum politikasıyla gözden geçirilmelidir.
  - Kod: `rules/core.yaml`, `app/scoring/engine.py`.
- **Süre (R2 md. 28/2).** "Şüpheli işlemler, işleme ilişkin şüphenin oluştuğu tarihten itibaren en geç on iş günü içinde Başkanlığa bildirilir." Her vakada `masak_deadline` ve kalan iş günü tutulur; worker ≤ 2 iş günü kalan vakaları (`fraud_cases_masak_due_soon`) ve süresi geçmiş vakaları (`fraud_cases_masak_overdue`) ayrı raporlar.
  - Kod: `app/cases/sla.py`. Resmî tatiller her yıl için `holidays.Turkey` ile hesaplanır (dini bayramlar dahil). Arife yarım günleri `tr_half_day_policy` ayarına bağlıdır: varsayılan `business_day` (sabah çalışılır, bu yüzden son tarih hiçbir zaman yasal süreden geç hesaplanmaz) ya da `holiday`. Ek kapanışlar `tr_holidays` ile eklenir.
- **Bildirimin açıklanamaması / tipping-off (R1 md. 4/2, R2 md. 29/1).** Bildirimde bulunulduğu taraflara açıklanamaz ve bildirim gizlidir.
  - Kod: `app/scoring/engine.py::typology_cap` ve `app/scoring/policy.py` (`TYPOLOGY_CAP`): AML örüntülerinde müşteriye görünen BLOCK yerine HOLD uygulanır. `app/copilot/templates.py::TIPPING_OFF` uyarısı her ŞİB taslağına eklenir.
- **Onay.** ŞİB gönderimi maker-checker ister (`SIB_TASLAK → SIB_ONAY_BEKLIYOR → SIB_ONAYLANDI / SIB_GONDERILDI`); talebi açan kişi onaylayamaz. Bu bir kurum içi kontrol tasarımıdır; bu yapının mevzuatta zorunlu olduğu doğrulanmadı.

## KVKK — 6698 sayılı Kanun (R4)

Aşağıdaki önlemler kişisel verinin işlenmesini en aza indirmek için alındı. Belirli bir KVKK maddesine eşlenmedi.

- LLM'e giden veride TCKN (checksum ile), IBAN, telefon, e-posta ve isimler pseudonimleştirilir, yanıt geri eşlenir. ASCII'ye katlanmış ve büyük harfli isim biçimleri ile alıcı adları da kapsanır.
  - Kod: `app/llm/redaction.py`.
- Konsorsiyum demosunda tanımlayıcılar yalnızca tuzlu SHA-256 özetiyle paylaşılır.
  - Kod: `app/consortium.py`.
- Anahtarlar ve gizli değerler log filtresiyle maskelenir; `.env` repoya ve imaja girmez.

## BDDK — Bilgi Sistemleri Yönetmeliği (R3)

Aşağıdaki kontroller bu yönetmeliğin genel amaçları (denetim izi, erişim kontrolü, görevler ayrılığı) dikkate alınarak tasarlandı. Madde bazında eşleme yapılmadı ve **doğrulanmadı**.

- Hash-zincirli değiştirilemez denetim izi (`GET /api/audit/verify`). Kod: `app/db/audit.py`.
- Rol bazlı erişim (`analist < kidemli_analist < admin`), dört göz ilkesi (bloke kaldırma, ŞİB, model terfisi). Kod: `app/security/`, `app/cases/service.py`.
- Karar başına model/kural versiyonu ve gecikme kaydı.
- Servis ingest'inde HMAC imzası ve tek kullanımlık `X-Nonce`; SSE için tek kullanımlık bilet; `/metrics` için `METRICS_TOKEN`. Kod: `app/security/auth.py`, `app/security/tickets.py`, `app/api/routes/health.py`.

## APP dolandırıcılığı bağlamı (R5, R6)

- **Birleşik Krallık (R6).** PSR, PS23/3 ile Faster Payments'ta APP dolandırıcılığı için zorunlu geri ödeme getirdi. PS24/7 (02.10.2024) azami geri ödemeyi 7 Ekim 2024'ten itibaren talep başına £85,000 olarak teyit etti. Bu düzenleme bankaları APP dolandırıcılığını ödeme anında önlemeye yönlendiriyor.
- **AB (R5).** PSD3 ve PSR metinleri Komisyon önerisidir. Avrupa Parlamentosu Legislative Train sayfasına göre (durum 1 Ağustos 2026) geçici uzlaşmaya 27 Kasım 2025'te varıldı, ECON 5 Mayıs 2026'da onayladı. Resmî kabul ve Resmî Gazete'de yayım **doğrulanmadı**; metinler yürürlükteki hukuk olarak ele alınmamalıdır.
- **Platformdaki karşılık.** Confirmation of Payee, dinamik risk uyarıları ve cooling-off HOLD uygulanır; müşterinin "Bu kişiyi tanıyor musunuz?" yanıtı vakaya kanıt olarak eklenir. Kod: `app/app_scam/cop.py`, `app/app_scam/engine.py`. Geri ödeme süreci ve tutar sınırı platformda **uygulanmadı**.

## Yüksek riskli ülkeler (R7)

- `high_risk_countries` elle yazılmış bir dize değildir; `data/jurisdictions/fatf_2026-06.json` dosyasından yüklenir (versiyon `fatf-2026-06`, genel kurul 17–19 Haziran 2026). Kod: `app/core/jurisdictions.py`, `app/config.py`.
- **Kara liste** (call for action: IR, KP, MM): fatf-gafi.org 2026-09-25'te 403 döndürdüğü için, FATF bildirisini yeniden yayımlayan Singapur Para Otoritesi (MAS) sayfasıyla doğrulandı.
- **Gri liste** (increased monitoring): FATF sayfası okunamadı; liste iki ikincil kaynaktan derlendi. **Doğrulanmadı.** Üretimde FATF sayfasından güncellenmelidir.

## Yaptırım taraması

Tam ve bulanık ad eşleşmesi, blocking indeksi ve ikincil anahtarlar (doğum yılı, uyruk) ile eşleşme güveni hesaplanır. Kod: `app/core/sanctions.py`. Repodaki yaptırım listesi kurgusaldır; resmî bir listeye (BM, OFAC, AB, MASAK) bağlı değildir.
