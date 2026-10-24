from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from strategies.options_kernel.types import PluginRuntimeState


_DEFAULT_DAILY_ATR_PATH = str(
    Path(__file__).resolve().parents[2] / "runtime" / "state" / "qqq_daily_atr_context.json"
)
_DEFAULT_DAILY_ATR_PATH_BY_SYMBOL = {
    "QQQ": _DEFAULT_DAILY_ATR_PATH,
    "SPY": str(Path(__file__).resolve().parents[2] / "runtime" / "state" / "spy_daily_atr_context.json"),
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return float(default)


@dataclass(frozen=True)
class _MinuteRow:
    ts: datetime
    close: float
    volume: float
    vwap: float
    high: float = 0.0
    low: float = 0.0


@dataclass(frozen=True)
class QQQUnderlyingTruthFabric:
    now: datetime
    symbol: str
    spot: float
    vwap: float
    qqew_now: Optional[float]
    qqew_vwap_now: Optional[float]
    qqq_ago: Optional[float]
    qqew_ago: Optional[float]
    v3m_signed: float
    atr_live: float
    atr_seed: float
    atr_eff: float
    atr_cap: float
    vol_t: float
    vol_ma10: Optional[float]
    vol_surge_ratio: float
    distance_from_vwap_atr: float
    data_age_qqq_sec: Optional[float]
    data_age_qqew_sec: Optional[float]
    continuity_180s_ok: bool
    close_by_minute: Dict[datetime, float] = field(default_factory=dict)
    vwap_by_minute: Dict[datetime, float] = field(default_factory=dict)
    vol_surge_by_minute: Dict[datetime, float] = field(default_factory=dict)
    live_samples: list[tuple[datetime, float]] = field(default_factory=list)
    session_open_price: Optional[float] = None
    day_position_rank: Optional[float] = None
    qqew_decoupled: bool = False
    extras: Dict[str, Any] = field(default_factory=dict)

    def as_signal_payload(self) -> Dict[str, Any]:
        return {
            "spot": float(self.spot),
            "vwap": float(self.vwap),
            "qqew_now": self.qqew_now,
            "qqew_vwap_now": self.qqew_vwap_now,
            "v3m_signed": float(self.v3m_signed),
            "atr_live": float(self.atr_live),
            "atr_seed": float(self.atr_seed),
            "atr_eff": float(self.atr_eff),
            "atr_cap": float(self.atr_cap),
            "vol_surge_ratio": float(self.vol_surge_ratio),
            "distance_from_vwap_atr": float(self.distance_from_vwap_atr),
            "data_age_qqq_sec": self.data_age_qqq_sec,
            "data_age_qqew_sec": self.data_age_qqew_sec,
            "continuity_180s_ok": bool(self.continuity_180s_ok),
            "session_open_price": self.session_open_price,
            "day_position_rank": self.day_position_rank,
        }


def _symbol_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _symbol_slug(value: Any) -> str:
    return _symbol_key(value).lower()


def _feature_lookup(features: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in features and features.get(key) is not None:
            return features.get(key)
    return None


def _resolve_profile(runtime: PluginRuntimeState, extras: Dict[str, Any]) -> dict[str, str]:
    primary_symbol = _symbol_key(extras.get("primary_symbol") or runtime.symbol)
    confirm_symbol = _symbol_key(extras.get("confirm_symbol"))
    confirm_prefix = str(extras.get("confirm_feature_prefix") or _symbol_slug(confirm_symbol) or "qqew").strip().lower()
    return {
        "primary_symbol": primary_symbol or "QQQ",
        "primary_prefix": _symbol_slug(primary_symbol) or "qqq",
        "confirm_symbol": confirm_symbol,
        "confirm_prefix": confirm_prefix or "qqew",
    }


def _load_atr_rank_from_file(path: str, today: str) -> tuple[Optional[float], Optional[float]]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        days = [r for r in (data.get("days") or []) if str(r.get("day", "")) < today and float(r.get("atr14") or 0) > 0]
        if not days:
            return None, None
        days.sort(key=lambda x: str(x.get("day", "")))
        atr_prev = float(days[-1]["atr14"])
        win = [float(r["atr14"]) for r in days[-30:]]
        leq = sum(1 for v in win if v <= atr_prev)
        rank = float(leq / max(1, len(win)))
        return rank, atr_prev
    except Exception:
        return None, None


def _normalize_history(runtime: PluginRuntimeState) -> list[_MinuteRow]:
    rows = []
    for item in list(runtime.payload.get("bar_history") or []):
        ts = item.get("ts")
        if not isinstance(ts, datetime):
            continue
        close = _safe_float(item.get("close"))
        if close <= 0:
            continue
        rows.append(
            _MinuteRow(
                ts=ts.replace(second=0, microsecond=0),
                close=close,
                volume=max(0.0, _safe_float(item.get("volume"), 0.0)),
                vwap=_safe_float(item.get("vwap", close), close),
                high=max(close, _safe_float(item.get("high"), close)),
                low=min(close, max(1e-9, _safe_float(item.get("low"), close))),
            )
        )
    bar_ts = runtime.now.replace(second=0, microsecond=0)
    spot = _safe_float(runtime.spot or runtime.bar.close)
    if spot > 0 and (not rows or rows[-1].ts != bar_ts):
        rows.append(
            _MinuteRow(
                ts=bar_ts,
                close=spot,
                volume=max(0.0, _safe_float(runtime.bar.volume, 0.0)),
                vwap=_safe_float(runtime.bar.vwap, spot),
                high=max(spot, _safe_float(runtime.bar.high if hasattr(runtime.bar, "high") else spot, spot)),
                low=min(spot, max(1e-9, _safe_float(runtime.bar.low if hasattr(runtime.bar, "low") else spot, spot))),
            )
        )
    return rows[-390:]


def _value_ago(rows: Iterable[_MinuteRow], now_ts: datetime, minutes: int) -> Optional[float]:
    target = now_ts - timedelta(minutes=max(0, int(minutes)))
    best: Optional[_MinuteRow] = None
    for row in rows:
        if row.ts > target:
            continue
        if best is None or row.ts > best.ts:
            best = row
    return None if best is None else float(best.close)


def _compute_atr_from_rows(rows: list[_MinuteRow], period: int = 14) -> Optional[float]:
    if len(rows) < period + 1:
        return None
    trs = []
    prev_close = rows[0].close
    for row in rows[1:]:
        h = row.high if row.high > 0 else row.close
        lo = row.low if 0 < row.low <= row.close else row.close
        tr = max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = row.close
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / float(period)
    for x in trs[period:]:
        atr = (atr * (period - 1) + x) / float(period)
    return float(atr) if atr > 0 else None


def _rolling_avg_volume(rows: list[_MinuteRow], length: int = 10) -> Optional[float]:
    if len(rows) < max(1, int(length)):
        return None
    subset = rows[-int(length) :]
    if not subset:
        return None
    total = sum(float(item.volume) for item in subset)
    return float(total / max(1, len(subset)))


def _session_pos_day(rows: list[_MinuteRow], *, now: datetime, spot: float) -> Optional[float]:
    session_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now <= session_start:
        return None
    prices = [float(row.close) for row in rows if row.ts.date() == now.date() and session_start <= row.ts <= now]
    if not prices:
        return None
    prices.append(float(spot))
    day_low = min(prices)
    day_high = max(prices)
    if day_high - day_low <= 1e-9:
        return 0.5
    return float((float(spot) - day_low) / max(day_high - day_low, 1e-9))


def resolve_qqq_truth_fabric(runtime: PluginRuntimeState, extras: Dict[str, Any]) -> QQQUnderlyingTruthFabric:
    out_extras = dict(extras or {})
    now = runtime.now
    profile = _resolve_profile(runtime, out_extras)
    primary_symbol = profile["primary_symbol"]
    primary_prefix = profile["primary_prefix"]
    confirm_symbol = profile["confirm_symbol"]
    confirm_prefix = profile["confirm_prefix"]

    if out_extras.get("atr_rank_today") is None and out_extras.get("atr_rank_prev_day") is None:
        daily_atr_path = str(
            out_extras.get("daily_atr_path")
            or out_extras.get(f"{primary_prefix}_daily_atr_path")
            or out_extras.get("qqq_daily_atr_path")
            or os.getenv(f"{primary_symbol}_DAILY_ATR_PATH")
            or os.getenv("QQQ_DAILY_ATR_PATH")
            or _DEFAULT_DAILY_ATR_PATH_BY_SYMBOL.get(primary_symbol, _DEFAULT_DAILY_ATR_PATH)
        )
        today_str = now.strftime("%Y-%m-%d")
        atr_rank, atr_d = _load_atr_rank_from_file(daily_atr_path, today_str)
        if atr_rank is not None:
            out_extras["atr_rank_prev_day"] = atr_rank
        if atr_d is not None and out_extras.get("atr_d_prev_day") is None:
            out_extras["atr_d_prev_day"] = atr_d

    rows = _normalize_history(runtime)
    now_minute = now.replace(second=0, microsecond=0)
    spot = _safe_float(runtime.spot or runtime.bar.close)
    qqq_ago = _value_ago(rows, now_minute, 3)
    feature_v3m = runtime.features.get("v3m_signed", runtime.features.get("v3m"))
    v3m_signed = float(feature_v3m) if feature_v3m is not None else (float(spot - qqq_ago) if qqq_ago is not None else 0.0)
    vwap = _safe_float(runtime.features.get("vwap", runtime.bar.vwap or spot), spot)

    qqew_now_raw = _feature_lookup(
        runtime.features,
        "confirm_now",
        "confirm_close_now",
        f"{confirm_prefix}_now",
        f"{confirm_prefix}_close_now",
        "qqew_now",
        "qqew_close_now",
    )
    qqew_vwap_raw = _feature_lookup(
        runtime.features,
        "confirm_vwap_now",
        "confirm_vwap",
        f"{confirm_prefix}_vwap_now",
        f"{confirm_prefix}_vwap",
        "qqew_vwap_now",
        "qqew_vwap",
    )
    qqew_ago_raw = _feature_lookup(
        runtime.features,
        "confirm_ago",
        f"{confirm_prefix}_ago",
        "qqew_ago",
    )
    qqew_now = float(qqew_now_raw) if qqew_now_raw is not None else None
    qqew_vwap_now = float(qqew_vwap_raw) if qqew_vwap_raw is not None else None
    qqew_ago = float(qqew_ago_raw) if qqew_ago_raw is not None else None
    qqew_decoupled = bool(
        out_extras.get("confirm_decoupled", out_extras.get("qqew_decoupled", runtime.features.get("confirm_decoupled", runtime.features.get("qqew_decoupled", False))))
    )

    atr_period = max(5, int(out_extras.get("atr_period", 14)))
    atr_computed = _compute_atr_from_rows(rows, period=atr_period)
    atr_live = max(
        0.0,
        _safe_float(_feature_lookup(runtime.features, "atr_live", f"atr_1m_{primary_prefix}", "atr_1m_primary", "atr_1m_qqq"), 0.0),
        _safe_float(runtime.features.get("atr_eff", None), 0.0),
        _safe_float(runtime.bar.atr, 0.0),
        _safe_float(atr_computed, 0.0),
    )
    atr_seed = max(
        0.0,
        _safe_float(_feature_lookup(runtime.features, "atr_seed", f"atr_seed_{primary_prefix}", "atr_seed_primary", "atr_seed_tqqq"), atr_live),
    )
    atr_cap_mult = max(0.1, _safe_float(out_extras.get("atr_cap_mult", 1.8), 1.8))
    atr_cap = atr_cap_mult * atr_seed
    atr_eff = min(atr_live, atr_cap) if atr_live > 0 and atr_cap > 0 else max(atr_live, atr_seed, 0.0)

    vol_t = max(0.0, _safe_float(runtime.bar.volume, 0.0))
    vol_ma10 = _rolling_avg_volume(rows, 10)
    if vol_ma10 is None:
        vol_ma10 = _safe_float(runtime.features.get("vol_ma10", 0.0), 0.0) or None
    vol_surge_ratio = float(vol_t / max(float(vol_ma10 or 0.0), 1e-9)) if vol_ma10 and vol_ma10 > 0 else 0.0
    distance_from_vwap_atr = abs(float(spot - vwap)) / max(float(atr_eff), 1e-9) if atr_eff > 0 else 0.0
    day_position_rank = _feature_lookup(runtime.features, "day_position_rank", "pos_day")
    if day_position_rank is None:
        day_position_rank = _session_pos_day(rows, now=now, spot=spot)

    close_by_minute = {row.ts: float(row.close) for row in rows}
    vwap_by_minute = {row.ts: float(row.vwap) for row in rows}
    vol_surge_by_minute = {}
    for row in rows:
        ma = _rolling_avg_volume([r for r in rows if r.ts <= row.ts], 10)
        vol_surge_by_minute[row.ts] = float(row.volume / max(float(ma or 0.0), 1e-9)) if ma and ma > 0 else 0.0

    return QQQUnderlyingTruthFabric(
        now=now,
        symbol=str(runtime.symbol).upper(),
        spot=float(spot),
        vwap=float(vwap),
        qqew_now=qqew_now,
        qqew_vwap_now=qqew_vwap_now,
        qqq_ago=None if qqq_ago is None else float(qqq_ago),
        qqew_ago=qqew_ago,
        v3m_signed=float(v3m_signed),
        atr_live=float(atr_live),
        atr_seed=float(atr_seed),
        atr_eff=float(atr_eff),
        atr_cap=float(atr_cap),
        vol_t=float(vol_t),
        vol_ma10=None if vol_ma10 is None else float(vol_ma10),
        vol_surge_ratio=float(vol_surge_ratio),
        distance_from_vwap_atr=float(distance_from_vwap_atr),
        data_age_qqq_sec=_feature_lookup(runtime.features, "data_age_primary_sec", f"data_age_{primary_prefix}_sec", "data_age_qqq_sec"),
        data_age_qqew_sec=_feature_lookup(runtime.features, "data_age_confirm_sec", f"data_age_{confirm_prefix}_sec", "data_age_qqew_sec"),
        continuity_180s_ok=bool(runtime.features.get("continuity_180s_ok", True)),
        close_by_minute=close_by_minute,
        vwap_by_minute=vwap_by_minute,
        vol_surge_by_minute=vol_surge_by_minute,
        live_samples=[(row.ts, float(row.close)) for row in rows],
        session_open_price=runtime.features.get("session_open_price"),
        day_position_rank=day_position_rank,
        qqew_decoupled=bool(qqew_decoupled),
        extras={
            **out_extras,
            "primary_symbol": primary_symbol,
            "confirm_symbol": confirm_symbol,
            "confirm_feature_prefix": confirm_prefix,
        },
    )
