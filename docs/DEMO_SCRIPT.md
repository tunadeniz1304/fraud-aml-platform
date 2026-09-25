# 3 dakikalık demo akışı

1. **(0:00) Başlat** — `docker compose up --build`; logda `LLM: CANLI (...)` veya `LLM: DEMO modu`. `http://localhost:8000` → `analist / analist123`.
2. **(0:20) Canlı akış** — simülatör trafiği SSE ile akıyor; TPS ve **p99 skor gecikmesi** (hedef < 50 ms) sayaçları, renkli karar rozetleri.
3. **(0:45) Senaryo: ATO** — Senaryo sekmesi → "Hesap ele geçirme" → **BLOCK**; nedenler: yeni cihaz + VPN + ortalamanın ~10 katı + model sinyalleri.
4. **(1:05) Senaryo: APP** — yaşlı müşteri, aktif telefon görüşmesi, "güvenli hesap" açıklaması, CoP uyuşmazlığı → **HOLD** + müşteriye gösterilen dinamik uyarı.
5. **(1:25) Senaryo: Mule halkası** — 3 kurban → mule → 20 dk içinde nakit hesabına %92 → **HOLD**, vaka `MULE`, halka `RING-…` ("3 hesap, 1 paylaşılan cihaz, toplam …").
6. **(1:50) Vaka detayı** — reason code'lar + **SHAP şelalesi**, **Cytoscape ağ grafiği**, zaman çizelgesi. Copilot → "Özetle" (5 atıflı madde, araç izi), "Karar öner", sohbet: "Bu hesap neden riskli?" (SSE).
7. **(2:20) Smurfing + ŞİB** — eşik altı 4 transfer → **HOLD + AML vakası**; otomatik ŞİB taslağı → FRAUD kararı → "Onaya gönder" → `kidemli_analist` ile onay (maker-checker) → `SIB_GONDERILDI`, PDF indir.
8. **(2:45) Model izleme** — champion vs challenger (gölge skorlama), PSI drift, LLM kullanımı; admin "terfi" talebi başka kıdemli kullanıcının onayını bekler.
