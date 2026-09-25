# Uyum notları (kısa)

## MASAK — 5549 sayılı Kanun, Şüpheli İşlem Bildirimi (ŞİB)
- **Süre:** şüpheye ulaşıldığı andan itibaren **10 iş günü** (hafta sonu ve resmi tatiller hariç; `TR_HOLIDAYS`). Her vakada `masak_deadline` ve kalan iş günü tutulur; worker ≤ 2 iş günü kalan vakaları alarm olarak raporlar (`fraud_cases_masak_due_soon`).
- **İçerik:** kim / ne / ne zaman / nerede / neden, şüpheli işlem tipi, işlem listesi ve tutarlar — copilot taslağı (`SibDraft` şeması) + PDF/JSON dışa aktarım.
- **Onay:** ŞİB gönderimi maker-checker ister (`SIB_TASLAK → SIB_ONAY_BEKLIYOR → SIB_ONAYLANDI / SIB_GONDERILDI`); talebi açan kişi onaylayamaz.
- **Tipping-off yasağı (md. 4/2):** bildirim bilgisi taraflara açıklanamaz. Platformda AML (parçalama/katmanlama) örüntülerinde görünür BLOCK yerine **HOLD** uygulanır ve ŞİB taslağına uyarı eklenir.

## KVKK — 6698 sayılı Kanun
- LLM'e giden veride TCKN (checksum), IBAN, telefon, e-posta ve isimler pseudonimleştirilir, yanıt geri eşlenir (veri minimizasyonu, yurt dışı aktarım riski).
- Konsorsiyum paylaşımında tanımlayıcılar yalnız **tuzlu SHA-256** özetiyle paylaşılır.
- Anahtarlar ve gizli değerler log filtresiyle maskelenir; `.env` repoya ve imaja girmez.

## BDDK / BS yönetmeliği
- Hash-zincirli değiştirilemez denetim izi (`GET /api/audit/verify`), rol bazlı erişim, dört göz ilkesi, karar başına model/kural versiyonu ve gecikme kaydı.

## PSD3 / APP geri ödeme bağlamı
- AB'de PSD3/PSR ve Birleşik Krallık'ta PSR zorunlu APP geri ödemesi, bankaları APP dolandırıcılığını ödeme anında önlemeye itiyor: Confirmation of Payee, dinamik risk uyarıları ve cooling-off bekletmesi bu platformda uygulanır; müşteri "Bu kişiyi tanıyor musunuz?" yanıtı vakaya kanıt olarak eklenir.
