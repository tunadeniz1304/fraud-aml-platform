# ADR 0005 — KVKK: LLM'e giden veride pseudonimleştirme

- **Durum:** Kabul edildi (F1)
- **Bağlam:** Harici LLM sağlayıcısına kişisel veri (TCKN, IBAN, telefon, e-posta, ad-soyad) aktarımı KVKK md. 9 (yurt dışı aktarım) ve veri minimizasyonu ilkesi açısından risklidir.
- **Karar:** `app/llm/redaction.py` her canlı çağrıdan önce TCKN (checksum doğrulamalı), IBAN (`IBAN_…1234`), telefon, e-posta ve bilinen isimleri (`MUSTERI_n`) pseudonimleştirir; yanıt (akış dahil, parça sınırlarında) geri eşlenir. Anahtar hiçbir log/yanıt/hata metninde görünmez (`SecretStr`, log scrub filtresi).
- **Sonuçlar:** Testler hiçbir PII'nin tel üzerinden gitmediğini doğrular; demo modunda veri süreç dışına çıkmaz.
