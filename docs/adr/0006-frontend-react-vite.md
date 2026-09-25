# ADR 0006 — Analist konsolu: React + Vite + TypeScript + Tailwind

- **Durum:** Kabul edildi (F7)
- **Seçenekler:** React+Vite+TS+Tailwind (shadcn tarzı) veya HTMX + Alpine.js.
- **Karar:** **React + Vite + TypeScript + Tailwind**; bileşenler shadcn tarzında elle yazıldı (çalışma zamanı UI bağımlılığı yok), ağ grafiği için Cytoscape.js. Build Docker multi-stage ile üretilir ve FastAPI tarafından `/` (SPA) altında sunulur; klasik pano `/legacy`'de korunur.
- **Neden:** Katı CSP (`script-src 'self'`, `unsafe-inline` yok) ile uyum: Alpine.js ifadeleri `unsafe-eval` ister; React derlenmiş statik dosyalarla bunu gerektirmez. Tip güvenliği, vaka detayı gibi zengin ekranlar (SHAP şelalesi, graf, SSE sohbet) ve portföy okunabilirliği.
- **Sonuçlar:** Canlı akış SSE (`/api/live/stream`), copilot sohbeti SSE; koyu/açık tema, mobil uyumlu grid, klavye odak halkaları.
