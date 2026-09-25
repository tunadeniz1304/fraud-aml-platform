# ADR 0003 — Kademeli aksiyon için ayrı politika katmanı

- **Durum:** Kabul edildi (F3/F5)
- **Bağlam:** "Skor ≥ eşik → bloke" ikili karar, APP mağdurlarını cezalandırır, AML şüphelilerini uyarır (tipping-off) ve müşteri deneyimini bozar.
- **Karar:** Skor ile aksiyon ayrıldı: `PolicyEngine` kalibre riski **ALLOW / STEP_UP / HOLD / BLOCK**'a eşler; eşikler konfigürasyonda ve çalışma anında (admin, denetim kaydıyla) değişir. Deterministik override'lar: hesap BLOKE → BLOCK (skorlamadan önce), yaptırım → en az HOLD + vaka, bilinmeyen müşteri → HOLD, kural `action_hint` tabanı, **tipoloji tavanı** (APP/mule/AML → BLOCK yerine HOLD, hesap ele geçirme kanıtı yoksa).
- **Sonuçlar:** Hesap durum makinesi (`AKTIF → INCELENIYOR → BLOKE`) yalnız sistem yükseltmesi yapar; geri dönüş maker-checker ister. Senaryo testleri: ATO→BLOCK, APP→HOLD+uyarı, mule→HOLD+vaka+halka, smurfing→HOLD+AML vakası+ŞİB.
