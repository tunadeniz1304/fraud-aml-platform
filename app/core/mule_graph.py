"""Para katırı (money-mule) ağı tespiti.

Tek bir cihaz ya da alıcı (beneficiary) üzerinden birçok farklı müşterinin
işlem yapması, ya da tek bir müşterinin kısa bir pencere içinde birçok farklı
alıcıya para göndermesi — tipik "ring/sumlot" (mule) kalıbının işaretidir.

Bu modül, bu kalıpları kayan bir zaman penceresi içinde izleyen ve her işlem
için 0..1 aralığında bir risk puanı üreten saf, bağımsız bir `DeviceMuleGraph`
sunar. Modül hiçbir yan etki üretmez (import-safe).

Puanlama, "sıfır-yanlış-pozitif" yaklaşımı kullanır: cihaz/bir alıcı üzerinde
yalnızca 1 farklı müşteri görmek NORMALdir ve 0 puan verir; puan ancak eşiği
aşan farklı müşteri sayısı olduğunda yükselir (yetkisiz paylaşım => şüphe).
"""

from __future__ import annotations

from collections import defaultdict

# Fanout sinyali için varsayılan normalleştirme eşiği: tek müşterinin
# pencerede ulaştığı farklı alıcı sayısı bu değere ulaşınca puan 1 olur.
FANOUT_THRESHOLD = 3


def _normalised(distinct: int, threshold: int) -> float:
    """0 while ``distinct < threshold``; ramps 1/3..1 above it.

    A single distinct entity is the NORMAL case -> 0. Fraud only starts when
    the count crosses the threshold (else every dance transaction would score).
    """
    if distinct < threshold:
        return 0.0
    return min(1.0, (distinct - threshold + 1) / 3.0)


class DeviceMuleGraph:
    """Kısa bir pencere içinde cihaz/alıcı/müşteri etkileşimlerini izleyin.

    Her `update` çağrısında geçmişteki kayıtları (pencere dışına düşen) budar
    ve ilgili işlem için üç sinyal üretir:

    - `shared_device`: aynı cihazı paylaşan farklı müşteri sayısı (normalleştirilmiş)
    - `shared_beneficiary`: aynı alıcıyı paylaşan farklı müşteri sayısı (normalleştirilmiş)
    - `fanout`: tek müşterinin ulaştığı farklı alıcı sayısı (normalleştirilmiş)
    """

    def __init__(
        self,
        beneficiary_window_seconds: int = 3600,
        shared_device_threshold: int = 2,
        shared_beneficiary_threshold: int = 2,
        max_events: int = 50_000,
    ) -> None:
        self.beneficiary_window_seconds = beneficiary_window_seconds
        self.shared_device_threshold = shared_device_threshold
        self.shared_beneficiary_threshold = shared_beneficiary_threshold
        self.max_events = max_events
        # Pencere içinde tutulan işlem özetleri:
        #   (customer_id, device_id, beneficiary_id, ts_ms)
        self._events: list[tuple[str, str, str, float]] = []

    def update(self, tx: dict, ts_ms: float) -> dict[str, float]:
        """İşlemi kaydedip ilgili sinyal puanlarını döndürün."""
        record = self._extract(tx)
        zero = {"shared_device": 0.0, "shared_beneficiary": 0.0, "fanout": 0.0}
        if record is None:
            return zero
        customer, device, beneficiary = record

        self._events.append((customer, device, beneficiary, ts_ms))
        self._prune(ts_ms)

        devices, beneficiaries, customers = self._index()

        shared_device = _normalised(len(devices[device]), self.shared_device_threshold)
        shared_beneficiary = _normalised(
            len(beneficiaries[beneficiary]), self.shared_beneficiary_threshold
        )
        fanout = _normalised(len(customers[customer]), FANOUT_THRESHOLD)

        return {
            "shared_device": shared_device,
            "shared_beneficiary": shared_beneficiary,
            "fanout": fanout,
        }

    def score(self, tx: dict, ts_ms: float) -> float:
        """Üç sinyali ağırlıklı birleştirip 0..1 puan üretir."""
        scores = self.update(tx, ts_ms)
        return min(
            1.0,
            0.5 * scores["shared_device"]
            + 0.3 * scores["shared_beneficiary"]
            + 0.2 * scores["fanout"],
        )

    def _extract(self, tx: dict) -> tuple[str, str, str] | None:
        customer = tx.get("customer_id")
        device = tx.get("device_id")
        beneficiary = tx.get("beneficiary_id") or tx.get("beneficiary")
        if not customer or not device or not beneficiary:
            return None
        return str(customer), str(device), str(beneficiary)

    def _prune(self, ts_ms: float) -> None:
        """Pencerenin dışına düşen kayıtları atar."""
        cutoff = ts_ms - self.beneficiary_window_seconds * 1000
        self._events = [e for e in self._events if e[3] >= cutoff]
        if len(self._events) > self.max_events:  # hard memory bound (bug #11)
            self._events = self._events[-self.max_events :]

    def _index(
        self,
    ) -> tuple[
        dict[str, set[str]],
        dict[str, set[str]],
        dict[str, set[str]],
    ]:
        """Pencere içindeki kayıtlardan cihaz/alıcı/müşteri kümelerini kurar."""
        devices: dict[str, set[str]] = defaultdict(set)
        beneficiaries: dict[str, set[str]] = defaultdict(set)
        customers: dict[str, set[str]] = defaultdict(set)
        for customer, device, beneficiary, _ in self._events:
            devices[device].add(customer)
            beneficiaries[beneficiary].add(customer)
            customers[customer].add(beneficiary)
        return devices, beneficiaries, customers


def mule_explain(scores: dict) -> list[str]:
    """Sıfırdan büyük sinyaller için insan okunur açıklamalar üretir."""
    named = [
        (scores.get("shared_device", 0.0), "aynı cihazdan çok sayıda farklı müşteri"),
        (scores.get("shared_beneficiary", 0.0), "aynı alıcıya çok sayıda farklı müşteri"),
        (scores.get("fanout", 0.0), "tek müşteriden çok sayıda farklı alıcıya"),
    ]
    return [label for value, label in named if value > 0]
