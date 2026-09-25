# ADR 0004 — LLM karar vermez, önerir

- **Durum:** Kabul edildi (F1/F6)
- **Bağlam:** LLM'ler halüsinasyon üretebilir, gecikmeleri değişkendir ve regülasyon (BDDK, MASAK, KVKK) otomatik kararların açıklanabilir ve denetlenebilir olmasını ister.
- **Karar:** Senkron skor yolu LLM'e hiç bağlı değildir. LLM yalnız **async** işlerde kullanılır: vaka özeti, karar önerisi, ŞİB taslağı, analist sohbeti, APP metin sınıflandırması (yalnız vaka zenginleştirme, skor değil). Copilot salt-okunur araçlarla kanıt toplar; her çıktı pydantic şemasıyla ve **atıf doğrulamasıyla** (yalnız dosyadaki kanıt kimlikleri) kabul edilir. Bloke kaldırma, ŞİB gönderimi ve model terfisi **maker-checker** ister.
- **Sonuçlar:** Canlı çağrı başarısız olursa (timeout/429/5xx/geçersiz JSON/atıf hatası) deterministik demo çıktısı döner (`llm_mode="fallback"`); sistem asla çökmez ve anahtar olmadan tüm akışlar çalışır.
