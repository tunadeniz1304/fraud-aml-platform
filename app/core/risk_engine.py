"""Risk scoring engine.

Computes a single risk score in ``[0, 1]`` for an incoming transaction by
combining weighted behavioural signals against the customer's historical
context:

* **amount**  — how far the transfer strays above the customer's average.
* **device**  — whether the device ID is one the customer has used before.
* **location**— whether the transaction originates from a known location.
* **time**    — whether it lands inside the customer's typical hours.
* **velocity**— how many transfers the customer received in the last hour.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import config


@dataclass(frozen=True)
class RiskFactors:
    """Normalised (0..1) contribution of each behavioural signal."""

    amount: float
    device: float
    location: float
    time: float
    velocity: float

    @property
    def score(self) -> float:
        return (
            config.WEIGHT_AMOUNT * self.amount
            + config.WEIGHT_DEVICE * self.device
            + config.WEIGHT_LOCATION * self.location
            + config.WEIGHT_TIME * self.time
            + config.WEIGHT_VELOCITY * self.velocity
        )


def _amount_score(amount: float, avg_amount: float) -> float:
    """0 when at/below average; ramps to 1 at (<= 10x) average."""
    if avg_amount <= 0:
        return 0.0
    ratio = amount / avg_amount
    if ratio <= 1.0:
        return 0.0
    return min(1.0, (ratio - 1.0) / 9.0)


def _device_score(device_id: str, known_device_ids: set[str]) -> float:
    return 0.0 if device_id in known_device_ids else 1.0


def _location_score(location: str, known_locations: set[str]) -> float:
    return 0.0 if location in known_locations else 1.0


def _time_score(hour: int, typical_hours: set[int]) -> float:
    return 0.0 if hour in typical_hours else 0.7


def _velocity_score(events_last_hour: int) -> float:
    if config.HIGH_VELOCITY_PER_HOUR <= 0:
        return 0.0
    return min(1.0, events_last_hour / config.HIGH_VELOCITY_PER_HOUR)


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
) -> RiskFactors:
    """Evaluate all behavioural signals for one transfer."""
    return RiskFactors(
        amount=_amount_score(amount, avg_amount),
        device=_device_score(device_id, known_device_ids),
        location=_location_score(location, known_locations),
        time=_time_score(hour, typical_hours),
        velocity=_velocity_score(events_last_hour),
    )
