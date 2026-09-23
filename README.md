# Siber Güvenlik ve Fraud Analisti Ajanı (Cybersecurity & Fraud Analyst Agent)

Bankacılık için **gerçek zamanlı, olay güdümlü (event-driven)** otonom çalışan
bir dolandırıcılık analizi ajanı. Şüpheli bir para transferi geldiğinde tek bir
kural uyarısı üretmez; **pub/sub olay veri yolu** üzerinden çok aşamalı bir
analiz hattı işletir, transferi vektör veritabanındaki geçmiş "normal" davranışla
**semantik olarak karşılaştırır (RAG)**, isteğe bağlı gerçek **LLM** ile
dolandırıcılık yorumu alır ve risk eşiğini aşan işlemlerde hesabın
`hesap_durumu` sütununu **BLOKE** yaparak denetim logu yazar.

## Mimari

```
 transaction.created      transaction.monitored      transaction.analyzed
   (asyncio pub/sub)                │                           │
        ┌──────────▼────────┐   ┌────▼────────────┐   ┌─────────▼────────┐
        │  İşlem Monitörü    │──▶│  Bağlam Analisti │──▶│  Aksiyon Ajanı   │
        │ TransactionMonitor │   │ ContextAnalyst   │   │ ActionAgent      │
        └───────────────────┘   └──────┬───────────┘   └─────────┬────────┘
                                       │ ChromaDB RAG + LLM      ▼
                                       └ semantik karşılaştırma  account.blocked
```

Ajanlar `EventBus` üzerinden pub/sub ile haberleşir:

| Ajan | Abone olduğu olay | Yayınladığı olay |
|------|-------------------|------------------|
| **TransactionMonitor** | `transaction.created` | `transaction.monitored` |
| **ContextAnalyst** | `transaction.monitored` | `transaction.analyzed` |
| **ActionAgent** | `transaction.analyzed` | `account.blocked` + SQLite güncelleme |

## Nasıl çalışır?

1. `main.py` mock `data/transactions.json` akışını `transaction.created`
   olayları olarak `EventBus`'a besler.
2. **TransactionMonitor** her transferi temel bütünlük kontrollerinden geçirir.
3. **ContextAnalyst** müşterinin `data/customers.json` profilini kullanarak
   kural tabanlı **risk skoru (0..1)** hesaplar, işlemi ChromaDB'deki gömülü
   geçmiş davranışla **semantik mesafe** üzerinden RAG ile karşılaştırır ve
   `.env`'de gerçek bir LLM sağlayıcısı yapılandırılmışsa analiz ister.
4. **ActionAgent** risk eşiğine göre:
   - `>= RISK_THRESHOLD` → `hesap_durumu = BLOKE` (SQLite) + denetim logu.
   - `>= WARNING_THRESHOLD` → `hesap_durumu = INCELENIYOR`.
   - aksi halde → `GECTI` kaydı.
5. **FastAPI dashboard** (`server.py`) aynı canlı pipeline'ı REST üzerinden
   anlık durum, bloke listesi, hesap durumları ve denetim logu olarak sunar.

## LLM Sağlayıcısı (sahte LLM yok)

`.env` (.env.example'dan kopyala):

```
LLM_PROVIDER=none                # none | openai | anthropic
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-3-5-haiku-latest
```

- `none` → LLM çağrısı yapılmaz; kural motoru + RAG semantik analiz çalışır.
- `openai` / `anthropic` → gerçek SDK anahtarla kullanılır; `is_fraud`,
  `fraud_type`, `explanation`, `recommended_action` JSON'u döner.
- Hiçbir koşulda sahte/mock tamamlama üretilmez.

## Kurulum & Çalıştırma

```bash
python -m pip install -r requirements.txt
copy .env.example .env            # sonra anahtarları düzenle

python main.py                    # 1) konsol simülasyonu (tüm hattı çalıştırır)
python -m pytest -q               # 2) uçtan uca pytest
python server.py                  # 3) FastAPI dashboard -> http://localhost:8000
```

### Docker

```bash
docker compose up --build
# -> http://localhost:8000 (./data ve ./logs host'ta kalıcıdır)
```

## API Uç Noktaları

| Metot | Yol | Açıklama |
|-------|-----|----------|
| GET | `/api/health` | Sağlık + aktif LLM sağlayıcısı |
| GET | `/api/status` | Canlı pipeline özeti (monitored/analyzed/blocked/passed) |
| GET | `/api/stats` | Online risk dağılımı + drift istatistikleri |
| GET | `/api/transactions` | Analiz edilen transfer listesi |
| GET | `/api/blocks` | Otonom bloke listesi |
| GET | `/api/accounts` | Hesap durumları (`hesap_durumu`) |
| GET | `/api/audit` | Denetim logu |
| POST | `/api/transactions` | Yeni işlem ingest (Pydantic strict) |
| GET | `/api/admin/accounts` | Tüm hesaplar (operatör konsolu) |
| POST | `/api/admin/accounts/{id}/status` | Manuel durum değişikliği (AKTIF/BLOKE/INCELENIYOR) |
| GET | `/api/admin/audit/export` | RFC-4180 CSV denetim dışa aktarımı |

## Klasör Yapısı

```
├── main.py                  # Konsol simülasyonu (orkestratör)
├── server.py                # FastAPI dashboard sunucu
├── app/
│   ├── config.py            # pydantic-settings (env'den okur)
│   ├── api/                 # FastAPI dashboard + Pydantic şemaları
│   ├── core/
│   │   ├── event_bus.py     # asyncio pub/sub
│   │   ├── risk_engine.py   # kural tabanlı risk skoru
│   │   ├── behavior_store.py# ChromaDB vektör davranış deposu (RAG)
│   │   └── account_store.py # SQLite: hesap_durumu + audit_log
│   ├── agents/              # monitor -> analyst -> action
│   └── llm/                 # gerçek OpenAI/Anthropic istemci
├── data/                    # customers.json, transactions.json, chromadb, *.db
├── tests/                   # end-to-end pytest
└── scripts/                 # dashboard smoke test
```
