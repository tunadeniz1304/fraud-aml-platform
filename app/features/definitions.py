"""Feature registry (online/offline consistency).

Every model/rule feature is a pure function of a :class:`FeatureContext`
registered here with a Turkish description. The *same* functions run in the
real-time scorer and in the training backfill (which replays history through
an in-memory store), so there is no training/serving skew.

Windows are relative to the **event timestamp** (not wall clock) and only
count events strictly before the current one (point-in-time correctness).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable
from dataclasses import dataclass

from app.config import get_settings
from app.core.signals import purpose_keyword_score
from app.features.profile import channel_share, hour_probability
from app.features.types import FeatureContext, HistEvent

MINUTE, HOUR, DAY = 60.0, 3600.0, 86400.0
WEEK = 7 * DAY
_NO_HISTORY_GAP = WEEK  # time_since_last when the customer has no history


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    description: str
    fn: Callable[[FeatureContext], float]
    group: str = "davranış"


FEATURES: dict[str, FeatureDefinition] = {}


def feature(
    name: str, description: str, group: str = "davranış"
) -> Callable[[Callable[[FeatureContext], float]], Callable[[FeatureContext], float]]:
    def deco(fn: Callable[[FeatureContext], float]) -> Callable[[FeatureContext], float]:
        if name in FEATURES:  # pragma: no cover - programming error
            raise ValueError(f"feature '{name}' iki kez tanımlandı")
        FEATURES[name] = FeatureDefinition(name, description, fn, group)
        return fn

    return deco


# --- helpers ------------------------------------------------------------------------
def _stamps(ctx: FeatureContext) -> list[float]:
    stamps = ctx.cache.get("ts")
    if stamps is None:
        stamps = [e.ts for e in ctx.state.history]
        ctx.cache["ts"] = stamps
    return stamps


def _window(ctx: FeatureContext, seconds: float) -> list[HistEvent]:
    """Events in ``(now - seconds, now]`` — O(log n) via bisect, memoised."""
    key = f"w{seconds}"
    cached = ctx.cache.get(key)
    if cached is None:
        stamps = _stamps(ctx)
        now = ctx.tx.epoch
        lo = bisect.bisect_right(stamps, now - seconds)
        hi = bisect.bisect_right(stamps, now)
        cached = ctx.cache[key] = list(ctx.state.history[lo:hi])
    return cached  # type: ignore[no-any-return]


def _is_night(hour: int) -> bool:
    s = get_settings()
    return s.night_start_hour <= hour < s.night_end_hour


def _near_threshold(amount: float) -> bool:
    s = get_settings()
    return s.structuring_band * s.structuring_threshold_try <= amount < s.structuring_threshold_try


def _session_num(ctx: FeatureContext, key: str) -> float:
    value = ctx.tx.session.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else -1.0


def _session_flag(ctx: FeatureContext, key: str) -> float:
    return 1.0 if ctx.tx.session.get(key) is True else 0.0


# --- amount ---------------------------------------------------------------------------
@feature("amount_try", "İşlem tutarı (TRY'ye normalize)", "tutar")
def _amount_try(ctx: FeatureContext) -> float:
    return ctx.tx.amount_try


@feature("amount_log", "log(1 + tutar)", "tutar")
def _amount_log(ctx: FeatureContext) -> float:
    return math.log1p(ctx.tx.amount_try)


@feature("amount_ratio", "Tutarın müşterinin adaptif ortalamasına oranı", "tutar")
def _amount_ratio(ctx: FeatureContext) -> float:
    return ctx.tx.amount_try / max(1.0, math.expm1(ctx.profile.log_mean))


@feature("amount_zscore", "Tutarın müşterinin EWMA dağılımına göre z-skoru", "tutar")
def _amount_zscore(ctx: FeatureContext) -> float:
    std = math.sqrt(max(ctx.profile.log_var, 1e-6))
    return (math.log1p(ctx.tx.amount_try) - ctx.profile.log_mean) / std


@feature("first_large_transfer", "Müşterinin şimdiye kadarki en büyük transferini aşıyor", "tutar")
def _first_large(ctx: FeatureContext) -> float:
    factor = get_settings().large_transfer_factor
    return 1.0 if ctx.tx.amount_try > ctx.profile.max_amount * factor else 0.0


@feature("near_threshold", "Tutar raporlama eşiğinin hemen altında (yapılandırma)", "tutar")
def _near_threshold_f(ctx: FeatureContext) -> float:
    return 1.0 if _near_threshold(ctx.tx.amount_try) else 0.0


@feature("near_threshold_cnt_24h", "24 saatte eşik altı işlem sayısı (bu işlem dahil)", "hız")
def _near_threshold_cnt(ctx: FeatureContext) -> float:
    past = sum(1 for e in _window(ctx, DAY) if _near_threshold(e.amount))
    return float(past + (1 if _near_threshold(ctx.tx.amount_try) else 0))


# --- velocity -------------------------------------------------------------------------
def _count(seconds: float) -> Callable[[FeatureContext], float]:
    return lambda ctx: float(len(_window(ctx, seconds)))


def _sum(seconds: float) -> Callable[[FeatureContext], float]:
    return lambda ctx: float(sum(e.amount for e in _window(ctx, seconds)))


for _label, _secs in (("1m", MINUTE), ("1h", HOUR), ("24h", DAY), ("7d", WEEK)):
    FEATURES[f"cnt_{_label}"] = FeatureDefinition(
        f"cnt_{_label}", f"Son {_label} içindeki işlem adedi", _count(_secs), "hız"
    )
    FEATURES[f"sum_{_label}"] = FeatureDefinition(
        f"sum_{_label}", f"Son {_label} içindeki toplam tutar (TRY)", _sum(_secs), "hız"
    )


@feature("time_since_last_s", "Bir önceki işlemden bu yana geçen süre (sn)", "hız")
def _time_since_last(ctx: FeatureContext) -> float:
    past = _window(ctx, WEEK)
    return ctx.tx.epoch - past[-1].ts if past else _NO_HISTORY_GAP


# --- payee ------------------------------------------------------------------------------
@feature("distinct_payees_24h", "24 saatte farklı alıcı sayısı", "alıcı")
def _distinct_24h(ctx: FeatureContext) -> float:
    return float(len({e.beneficiary for e in _window(ctx, DAY) if e.beneficiary}))


@feature("distinct_payees_7d", "7 günde farklı alıcı sayısı", "alıcı")
def _distinct_7d(ctx: FeatureContext) -> float:
    return float(len({e.beneficiary for e in _window(ctx, WEEK) if e.beneficiary}))


@feature("is_new_payee", "Bu alıcıya ilk kez para gönderiliyor", "alıcı")
def _is_new_payee(ctx: FeatureContext) -> float:
    return 1.0 if ctx.tx.beneficiary and ctx.state.pair_first_seen is None else 0.0


@feature("payee_relation_age_d", "Alıcıyla ilk işlemden bu yana gün (-1: yeni)", "alıcı")
def _payee_relation_age(ctx: FeatureContext) -> float:
    first = ctx.state.pair_first_seen
    return -1.0 if first is None else (ctx.tx.epoch - first) / DAY


@feature("payee_fan_in_24h", "Alıcıya 24 saatte para gönderen farklı müşteri sayısı", "alıcı")
def _payee_fan_in(ctx: FeatureContext) -> float:
    if not ctx.tx.beneficiary:
        return 0.0
    return float(len(ctx.state.payee_senders_24h | {ctx.tx.customer_id}))


@feature("payee_age_d", "Alıcının sistemde ilk görülmesinden bu yana gün (-1: yeni)", "alıcı")
def _payee_age(ctx: FeatureContext) -> float:
    first = ctx.state.payee_first_seen
    return -1.0 if first is None else (ctx.tx.epoch - first) / DAY


# --- device / network ---------------------------------------------------------------------
@feature("device_missing", "İşlemde cihaz bilgisi yok (şube/ATM/eski kanal)", "cihaz")
def _device_missing(ctx: FeatureContext) -> float:
    return 0.0 if ctx.tx.device_id else 1.0


@feature("is_new_device", "Cihaz müşteri için yeni (cihaz bilgisi yoksa 0)", "cihaz")
def _is_new_device(ctx: FeatureContext) -> float:
    dev = ctx.tx.device_id
    if not dev:  # unknown is not "new": the missing indicator carries it
        return 0.0
    known = dev in ctx.customer.known_devices or dev in ctx.profile.devices
    return 0.0 if known else 1.0


@feature("device_age_d", "Cihazın sistemde ilk görülmesinden bu yana gün (-1: cihaz yok)", "cihaz")
def _device_age(ctx: FeatureContext) -> float:
    if not ctx.tx.device_id:
        return -1.0
    first = ctx.state.device_first_seen
    return 0.0 if first is None else (ctx.tx.epoch - first) / DAY


@feature("device_customers_7d", "Cihazı 7 günde kullanan farklı müşteri sayısı", "cihaz")
def _device_customers(ctx: FeatureContext) -> float:
    return float(len(ctx.state.device_customers_7d | {ctx.tx.customer_id}))


@feature("is_new_location", "Lokasyon müşterinin bilinen lokasyonları dışında", "cihaz")
def _is_new_location(ctx: FeatureContext) -> float:
    return 0.0 if ctx.tx.location in ctx.customer.known_locations else 1.0


@feature("ip_country_mismatch", "IP ülkesi ile işlem ülkesi uyuşmuyor", "cihaz")
def _ip_mismatch(ctx: FeatureContext) -> float:
    if ctx.ip is None or not ctx.tx.country:
        return 0.0
    return 1.0 if ctx.ip.country != ctx.tx.country else 0.0


@feature("is_anonymized_ip", "IP bilinen VPN/Tor aralığında", "cihaz")
def _anon_ip(ctx: FeatureContext) -> float:
    return 1.0 if ctx.ip is not None and ctx.ip.anonymised else 0.0


@feature("is_foreign", "İşlem ülkesi müşterinin ev ülkesi dışında", "cihaz")
def _is_foreign(ctx: FeatureContext) -> float:
    return 1.0 if ctx.tx.country and ctx.tx.country != ctx.customer.home_country else 0.0


@feature("is_high_risk_country", "İşlem ülkesi yüksek riskli yargı bölgesi", "cihaz")
def _high_risk_country(ctx: FeatureContext) -> float:
    return 1.0 if ctx.tx.country in get_settings().high_risk_country_set else 0.0


# --- time / channel ------------------------------------------------------------------------
@feature("hour", "İşlem saati (0-23)", "zaman")
def _hour(ctx: FeatureContext) -> float:
    return float(ctx.tx.ts.hour)


@feature("is_night", "Gece işlemi", "zaman")
def _is_night_f(ctx: FeatureContext) -> float:
    return 1.0 if _is_night(ctx.tx.ts.hour) else 0.0


@feature("night_ratio_7d", "Son 7 gündeki gece işlemlerinin oranı", "zaman")
def _night_ratio(ctx: FeatureContext) -> float:
    events = _window(ctx, WEEK)
    return sum(1 for e in events if _is_night(e.hour)) / len(events) if events else 0.0


@feature("hour_unusual", "Saat müşterinin adaptif saat dağılımında nadir", "zaman")
def _hour_unusual(ctx: FeatureContext) -> float:
    prob = hour_probability(ctx.profile, ctx.tx.ts.hour)
    return 1.0 if prob < get_settings().unusual_hour_prob else 0.0


@feature("is_cash_channel", "Nakit kanalı (ATM / nakit çekim)", "kanal")
def _is_cash_channel(ctx: FeatureContext) -> float:
    return 1.0 if ctx.tx.channel in get_settings().cash_channels else 0.0


@feature("channel_change", "Kanal bir önceki işlemden farklı", "kanal")
def _channel_change(ctx: FeatureContext) -> float:
    past = _window(ctx, WEEK)
    if not past or not ctx.tx.channel:
        return 0.0
    last = past[-1]
    return 1.0 if last.channel and last.channel != ctx.tx.channel else 0.0


@feature("channel_unknown", "Müşterinin kullanmadığı kanal", "kanal")
def _channel_unknown(ctx: FeatureContext) -> float:
    if not ctx.tx.channel:
        return 0.0
    known = ctx.tx.channel in ctx.customer.known_channels
    return 0.0 if known or channel_share(ctx.profile, ctx.tx.channel) > 0.05 else 1.0


# --- session / behavioural biometrics (lite) -------------------------------------------------
@feature("login_to_transfer_s", "Oturum açmadan transfere kadar geçen süre (sn, -1 yok)", "oturum")
def _login_to_transfer(ctx: FeatureContext) -> float:
    return _session_num(ctx, "login_to_transfer_s")


@feature("session_duration_s", "Oturum süresi (sn, -1 yok)", "oturum")
def _session_duration(ctx: FeatureContext) -> float:
    return _session_num(ctx, "session_duration_s")


@feature("typing_deviation", "Yazma ritminin müşteri profilinden sapması (z)", "oturum")
def _typing_deviation(ctx: FeatureContext) -> float:
    cadence = _session_num(ctx, "typing_cadence_ms")
    if cadence < 0 or ctx.profile.typing_n < 3:
        return 0.0
    std = math.sqrt(max(ctx.profile.typing_var, 1.0))
    return abs(cadence - ctx.profile.typing_mean) / std


@feature("paste_used", "Tutar/IBAN yapıştırıldı", "oturum")
def _paste(ctx: FeatureContext) -> float:
    return _session_flag(ctx, "paste_used")


@feature("is_emulator", "Emülatör tespit edildi", "oturum")
def _emulator(ctx: FeatureContext) -> float:
    return _session_flag(ctx, "is_emulator")


@feature("is_rooted", "Root/jailbreak cihaz", "oturum")
def _rooted(ctx: FeatureContext) -> float:
    return _session_flag(ctx, "is_rooted")


@feature("remote_access", "Uzaktan erişim aracı aktif", "oturum")
def _remote(ctx: FeatureContext) -> float:
    return _session_flag(ctx, "remote_access_tool")


@feature("active_call", "Transfer sırasında aktif telefon görüşmesi", "oturum")
def _call(ctx: FeatureContext) -> float:
    return _session_flag(ctx, "active_call")


# --- text / customer ---------------------------------------------------------------------------
@feature("purpose_risk", "Açıklamada aciliyet/sosyal mühendislik kalıbı skoru", "metin")
def _purpose_risk(ctx: FeatureContext) -> float:
    return purpose_keyword_score(ctx.tx.purpose)


@feature("vulnerable_customer", "Yaşlı/kırılgan müşteri", "müşteri")
def _vulnerable(ctx: FeatureContext) -> float:
    if ctx.customer.vulnerable:
        return 1.0
    year = ctx.customer.birth_year
    age = ctx.tx.ts.year - year if year else 0  # event time -> reproducible
    return 1.0 if age >= 65 else 0.0


#: Extra, externally computed signals (graph, CoP, consortium) — filled by the
#: scoring engine via ``FeatureContext.extras``; default 0 when not available.
EXTERNAL_FEATURES: dict[str, str] = {}


def register_external(name: str, description: str) -> None:
    EXTERNAL_FEATURES[name] = description


# Signals computed outside the feature store (graph, APP/CoP, online model).
# Declared here so the rule DSL can reference them; they are not ML inputs.
for _name, _desc in (
    ("graph_score", "Varlık grafı risk skoru (0-1)"),
    ("graph_pass_through", "30 dk içinde gelen paranın aktarılma oranı (fan-in → fan-out)"),
    ("graph_fan_in_30m", "Son 30 dakikada hesaba para gönderen farklı müşteri"),
    ("graph_cycle", "Para döngüsü (katmanlama) tespit edildi"),
    ("graph_fraud_distance", "Doğrulanmış fraud düğümüne mesafe (-1: yok)"),
    ("graph_known_mule_payee", "Alıcı doğrulanmış fraud/mule ağında"),
    ("app_score", "APP dolandırıcılığı skoru (0-1)"),
    ("cop_no_match", "Alıcı adı IBAN sahibiyle eşleşmiyor (CoP)"),
    ("cop_unknown", "Alıcı IBAN'ı CoP kaydında yok"),
    ("payee_account_age_d", "Alıcı hesabın açılışından bu yana gün (-1: bilinmiyor)"),
    ("online_anomaly", "Çevrimiçi (river HST) davranış anomali skoru"),
    ("consortium_hit", "Konsorsiyum ortak kara listesinde eşleşme"),
):
    register_external(_name, _desc)


def feature_names() -> list[str]:
    return [*FEATURES, *EXTERNAL_FEATURES]


def compute_features(ctx: FeatureContext) -> dict[str, float]:
    """All features (stable order) for one context."""
    out = {name: float(defn.fn(ctx)) for name, defn in FEATURES.items()}
    for name in EXTERNAL_FEATURES:
        out[name] = float(ctx.extras.get(name, 0.0))
    return out


def describe(name: str) -> str:
    if name in FEATURES:
        return FEATURES[name].description
    return EXTERNAL_FEATURES.get(name, name)
