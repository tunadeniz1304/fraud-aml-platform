# ADR 0002 — Hibrit skor: kural + GBM + anomali + harici sinyaller

- **Durum:** Kabul edildi (F3/F5)
- **Bağlam:** Tek başına kurallar yeni örüntüleri kaçırır ve bakım yükü yaratır; tek başına ML açıklanamaz ve soğuk başlangıçta zayıftır; etiketsiz yeni saldırıları anomali tespiti yakalar; mule ağları ve APP dolandırıcılığı işlem-dışı bağlam (graf, alıcı doğrulama) gerektirir.
- **Karar:** (1) Güvenli DSL ile YAML/DB **kural motoru** (noisy-OR, action floor), (2) **LightGBM** (46 feature, TreeSHAP açıklama), (3) **IsolationForest + ECOD** (yüzdelik kalibrasyon), (4) validasyon setinde eğitilen **lojistik stacker** — katsayılar ≥ 0 (monotonluk: hiçbir bileşen riski düşüremez; kural katsayısı ≥ 0.3), (5) **harici sinyaller** (varlık grafı, APP/CoP, river HST, konsorsiyum) `1-Π(1-w·s)` ile.
- **Neden:** Sektör devlerinin (Feedzai, ARIC, Radar) katmanlı yaklaşımı; her katman ayrı izlenir ve açıklanır (reason code kaynağı: rule/ml/signal/policy).
- **Sonuçlar:** Sentetik test setinde hibrit PR-AUC 0,971 (kural 0,68, ML 0,97, anomali 0,75); senkron p99 ≈ 2–11 ms. TreeSHAP yalnızca açıklama gereken kararlarda hesaplanır (gecikme bütçesi).
