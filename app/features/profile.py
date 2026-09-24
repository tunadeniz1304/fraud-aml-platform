"""Online EWMA profile updates (adaptive behavioural analytics).

Only *trusted* events update the profile — the caller passes ``learn=True``
for ALLOW decisions or analyst-labelled clean transactions. Fraudulent
transfers therefore cannot "teach" the profile that they are normal
(poisoning protection).
"""

from __future__ import annotations

import math

from app.features.types import ProfileState, TxView

MAX_DEVICES = 20
_VAR_FLOOR = 0.05


def learn(profile: ProfileState, tx: TxView, *, alpha_min: float) -> ProfileState:
    """Return a new profile updated with ``tx`` (the input is not mutated)."""
    p = profile.copy()
    alpha = max(alpha_min, 1.0 / (p.n + 1.0))
    x = math.log1p(max(0.0, tx.amount_try))
    diff = x - p.log_mean
    incr = alpha * diff
    p.log_mean += incr
    p.log_var = max(_VAR_FLOOR, (1 - alpha) * (p.log_var + diff * incr))
    p.max_amount = max(p.max_amount, tx.amount_try)

    p.hour_hist = [h * (1 - alpha) for h in p.hour_hist]
    p.hour_hist[tx.ts.hour] += 1.0
    p.channels = {c: v * (1 - alpha) for c, v in p.channels.items()}
    if tx.channel:
        p.channels[tx.channel] = p.channels.get(tx.channel, 0.0) + 1.0
    if tx.device_id:
        p.devices[tx.device_id] = tx.epoch
        if len(p.devices) > MAX_DEVICES:
            for stale in sorted(p.devices, key=p.devices.__getitem__)[
                : len(p.devices) - MAX_DEVICES
            ]:
                del p.devices[stale]

    typing = tx.session.get("typing_cadence_ms")
    if isinstance(typing, int | float) and typing > 0:
        ta = max(alpha_min, 1.0 / (p.typing_n + 1.0))
        tdiff = float(typing) - p.typing_mean
        tincr = ta * tdiff
        p.typing_mean += tincr
        p.typing_var = (1 - ta) * (p.typing_var + tdiff * tincr)
        p.typing_n += 1
    p.n += 1
    return p


def hour_probability(profile: ProfileState, hour: int) -> float:
    total = sum(profile.hour_hist)
    return profile.hour_hist[hour % 24] / total if total > 0 else 0.0


def channel_share(profile: ProfileState, channel: str) -> float:
    total = sum(profile.channels.values())
    return profile.channels.get(channel, 0.0) / total if total > 0 else 0.0
