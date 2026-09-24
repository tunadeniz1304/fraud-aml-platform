"""Behavioural rule-score engine (v1 signals, config-driven weights).

Computes a single score in ``[0, 1]`` by combining weighted behavioural and
context signals against the customer's historical profile:

* **amount**   — TRY-normalised amount vs. the customer's average (bug #12).
* **device**   — device never seen for this customer.
* **location** — location outside the customer's known locations.
* **time**     — transfer outside typical hours.
* **velocity** — transfers in the last hour.
* **country**  — high-risk / sanctioned jurisdiction (bug #6).
* **purpose**  — APP-scam / urgency keywords in the description (bug #6).
* **ip**       — IP country mismatch or VPN/Tor exit (bug #6).
* **channel**  — channel the customer has never used (bug #6).

All weights and saturation points come from :mod:`app.config`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.core.signals import IPIntel, default_ip_intel, purpose_keyword_score


@dataclass(frozen=True)
class RiskFactors:
    """Normalised (0..1) contribution of each behavioural signal."""

    amount: float
    device: float
    location: float
    time: float
    velocity: float
    country: float = 0.0
    purpose: float = 0.0
    ip: float = 0.0
    channel: float = 0.0

    @property
    def score(self) -> float:
        s = get_settings()
        return min(
            1.0,
            s.weight_amount * self.amount
            + s.weight_device * self.device
            + s.weight_location * self.location
            + s.weight_time * self.time
            + s.weight_velocity * self.velocity
            + s.weight_country * self.country
            + s.weight_purpose * self.purpose
            + s.weight_ip * self.ip
            + s.weight_channel * self.channel,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            name: round(getattr(self, name), 3)
            for name in (
                "amount",
                "device",
                "location",
                "time",
                "velocity",
                "country",
                "purpose",
                "ip",
                "channel",
            )
        }


def _amount_score(amount: float, avg_amount: float) -> float:
    """0 when at/below average; ramps to 1 at ``amount_saturation_ratio``x average."""
    if avg_amount <= 0:
        return 0.0
    ratio = amount / avg_amount
    if ratio <= 1.0:
        return 0.0
    saturation = max(1.0001, get_settings().amount_saturation_ratio)
    return min(1.0, (ratio - 1.0) / (saturation - 1.0))


def _device_score(device_id: str, known_device_ids: set[str]) -> float:
    return 0.0 if device_id in known_device_ids else 1.0


def _location_score(location: str, known_locations: set[str]) -> float:
    return 0.0 if location in known_locations else 1.0


def _time_score(hour: int, typical_hours: set[int]) -> float:
    return 0.0 if hour in typical_hours else get_settings().off_hours_score


def _velocity_score(events_last_hour: int) -> float:
    limit = get_settings().high_velocity_per_hour
    if limit <= 0:
        return 0.0
    return min(1.0, events_last_hour / limit)


def _country_score(country: str) -> float:
    """Elevated risk for sanctions/high-fraud country codes."""
    return 1.0 if (country or "").upper() in get_settings().high_risk_country_set else 0.0


def _ip_score(ip: str | None, tx_country: str, intel: IPIntel) -> float:
    info = intel.lookup(ip)
    if info is None:
        return 0.0
    if info.anonymised:
        return 1.0
    if tx_country and info.country != tx_country.upper():
        return 0.8
    return 0.0


def _channel_score(channel: str | None, known_channels: set[str] | None) -> float:
    if not channel or not known_channels:
        return 0.0
    return 0.0 if channel in known_channels else 1.0


_LABELS = {
    "amount": "tutar ortalamanın üzerinde",
    "device": "bilinmeyen cihaz",
    "location": "alışılmadık lokasyon",
    "time": "alışılmadık saat",
    "velocity": "yüksek işlem sıklığı",
    "country": "yüksek riskli ülke",
    "purpose": "açıklamada aciliyet/sosyal mühendislik ifadesi",
    "ip": "IP ülke uyumsuzluğu veya VPN/Tor",
    "channel": "müşterinin kullanmadığı kanal",
}


def explain_factors(factors: RiskFactors) -> list[str]:
    """Human-readable explanation of the non-zero signals, strongest first."""
    named = [(getattr(factors, key), label) for key, label in _LABELS.items()]
    return [label for value, label in sorted(named, key=lambda p: -p[0]) if value > 0]


def compute_risk(
    *,
    amount: float,
    avg_amount: float,
    device_id: str,
    known_device_ids: set[str],
    location: str,
    known_locations: set[str],
    hour: int,
    typical_hours: set[int],
    events_last_hour: int = 0,
    country: str = "",
    purpose: str = "",
    ip_address: str | None = None,
    channel: str | None = None,
    known_channels: set[str] | None = None,
    ip_intel: IPIntel | None = None,
) -> RiskFactors:
    """Evaluate all behavioural signals for one transfer (amounts in TRY)."""
    return RiskFactors(
        amount=_amount_score(amount, avg_amount),
        device=_device_score(device_id, known_device_ids),
        location=_location_score(location, known_locations),
        time=_time_score(hour, typical_hours),
        velocity=_velocity_score(events_last_hour),
        country=_country_score(country),
        purpose=purpose_keyword_score(purpose),
        ip=_ip_score(ip_address, country, ip_intel or default_ip_intel()),
        channel=_channel_score(channel, known_channels),
    )
