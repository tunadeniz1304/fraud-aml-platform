# ADR 0001 — Olay omurgası olarak Redis Streams

- **Durum:** Kabul edildi (F2)
- **Bağlam:** Skorlama hattı dış üreticilerden (core banking, ödeme anahtarı, simülatör) gelen işlemleri kayıpsız, tekrarsız ve sıralı işlemeli; tek komutla (`docker compose up`) ayağa kalkmalı ve laptop'ta çalışmalı.
- **Seçenekler:** Kafka (güçlü ama ZooKeeper/KRaft + JVM, demo için ağır), RabbitMQ (kuyruk semantiği, replay zayıf), Redis Streams (consumer group, ack, XAUTOCLAIM, düşük işletim maliyeti), yalnızca in-memory.
- **Karar:** Dış sınırda **Redis Streams** (ingress `fraud:transaction.created`, egress `fraud:decision.made`), süreç içi aşamalarda **InMemoryBus** (aynı arayüz). Idempotency anahtarı = `transaction_id`, sıralama anahtarı = `customer_id`, deneme sayacı + **dead-letter stream**, sınırlı kuyruk = backpressure.
- **Sonuçlar:** 10.000 işlemlik replay testinde kayıp/çift yok; zehirli mesaj DLQ'da görünür (`GET /api/bus/dlq`). Ölçek gerektiğinde arayüz korunarak Kafka'ya geçilebilir; sıcak yol süreç içinde kaldığı için gecikme ağ turu eklemez.
