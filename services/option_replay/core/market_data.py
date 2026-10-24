"""Market-data helpers for the option replay engine."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import math
import numpy as np
import pandas as pd

from services.option_replay.core.types import ContractQuote, ReplayConfig

try:
    from libs.db.ch_pool import get_client
except Exception:  # pragma: no cover
    get_client = None  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _to_ny_naive(ts_series: pd.Series) -> pd.Series:
    """Normalize mixed timestamp inputs to America/New_York naive datetimes.

    Replay comparisons are session-time based; using NY-local naive timestamps
    avoids UTC-shifted session windows and bar/quote key mismatches.
    """
    parsed = pd.to_datetime(ts_series, errors="coerce")
    if parsed.dt.tz is not None:
        return parsed.dt.tz_convert("America/New_York").dt.tz_localize(None)
    # Treat tz-naive source as UTC from storage layer, then convert to NY.
    return parsed.dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.tz_localize(None)


def resolve_indicator_profile(cfg: ReplayConfig | None = None) -> Dict[str, Any]:
    profile_name = str(getattr(cfg, "indicator_profile", "standard_close_rth_sma") or "standard_close_rth_sma").strip().lower()
    profiles: Dict[str, Dict[str, Any]] = {
        "standard_close_rth_sma": {
            "name": "standard_close_rth_sma",
            "price_source": "close",
            "ma_mode": "sma",
            "session_mode": "rth",
            "cross_day": True,
        },
        "standard_close_extended_sma": {
            "name": "standard_close_extended_sma",
            "price_source": "close",
            "ma_mode": "sma",
            "session_mode": "extended",
            "cross_day": True,
        },
    }
    if profile_name not in profiles:
        raise ValueError(f"unsupported indicator_profile: {profile_name}")
    profile = dict(profiles[profile_name])
    if cfg is not None:
        profile["price_source"] = str(getattr(cfg, "indicator_price_source", profile["price_source"]) or profile["price_source"]).strip().lower()
        profile["ma_mode"] = str(getattr(cfg, "indicator_ma_mode", profile["ma_mode"]) or profile["ma_mode"]).strip().lower()
        session_override = str(getattr(cfg, "market_data_session", profile["session_mode"]) or profile["session_mode"]).strip().lower()
        if session_override in {"rth", "extended"}:
            profile["session_mode"] = session_override
    if profile["price_source"] != "close":
        raise ValueError(f"unsupported indicator price source: {profile['price_source']}")
    if profile["ma_mode"] != "sma":
        raise ValueError(f"unsupported indicator ma mode: {profile['ma_mode']}")
    return profile


def clean_intraday_bars(df: pd.DataFrame, *, max_price_deviation_pct: float = 0.35, max_bar_range_pct: float = 0.20) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    out = df.copy().sort_values("ts").reset_index(drop=True)
    accepted_idx: list[int] = []
    accepted_closes: list[float] = []
    dev_limit = 1.0 + float(max_price_deviation_pct)

    for idx, row in out.iterrows():
        open_ = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        structurally_valid = (
            open_ > 0
            and high > 0
            and low > 0
            and close > 0
            and high >= low
            and high >= open_
            and high >= close
            and low <= open_
            and low <= close
        )
        if not structurally_valid:
            continue

        if accepted_closes:
            ref = float(pd.Series(accepted_closes[-20:]).median())
        else:
            ref = close
        if ref <= 0:
            ref = close

        prices = (open_, high, low, close)
        price_ok = all((p / ref) >= (1.0 / dev_limit) and (p / ref) <= dev_limit for p in prices)
        range_ok = ((high - low) / ref) <= float(max_bar_range_pct)
        if not (price_ok and range_ok):
            continue

        accepted_idx.append(idx)
        accepted_closes.append(close)

    return out.loc[accepted_idx].reset_index(drop=True)


def load_spy_bars(
    start_date: str,
    end_date: str,
    symbol: str = "QQQ",
    *,
    session_mode: str = "rth",
    quality_mode: str = "raw",
    max_price_deviation_pct: float = 0.35,
    max_bar_range_pct: float = 0.20,
) -> pd.DataFrame:
    if get_client is None:
        raise RuntimeError("ClickHouse client unavailable")
    session = str(session_mode or "rth").strip().lower()
    if session not in {"rth", "extended"}:
        raise ValueError(f"unsupported session_mode: {session_mode}")
    q = """
    SELECT
      ts_start AS ts,
      symbol,
      open,
      high,
      low,
      close,
      vwap,
      volume
    FROM trading.bars_1m_ibkr FINAL
    WHERE symbol = %(s)s
      AND (%(session)s = 'extended' OR is_rth = 1)
      AND ts_start >= %(start)s
      AND ts_start < %(end)s
    ORDER BY ts_start
    """
    ch = get_client()
    rows = ch.query(
        q,
        {"s": symbol, "start": f"{start_date} 00:00:00", "end": f"{end_date} 00:00:00", "session": session},
        settings={"use_query_cache": False},
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = _to_ny_naive(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    mode = str(quality_mode or "raw").strip().lower()
    if mode not in {"raw", "clean"}:
        raise ValueError(f"unsupported quality_mode: {quality_mode}")
    if mode == "clean":
        df = clean_intraday_bars(
            df,
            max_price_deviation_pct=max_price_deviation_pct,
            max_bar_range_pct=max_bar_range_pct,
        )
    return df


def infer_warmup_start_date(
    start_date: str,
    symbol: str = "QQQ",
    warmup_trading_days: int = 2,
    *,
    session_mode: str = "rth",
) -> str:
    if warmup_trading_days <= 0:
        return start_date
    if get_client is None:
        # Conservative fallback for offline mode.
        d0 = datetime.strptime(start_date, "%Y-%m-%d").date()
        return (d0 - timedelta(days=warmup_trading_days * 3)).isoformat()
    session = str(session_mode or "rth").strip().lower()
    if session not in {"rth", "extended"}:
        raise ValueError(f"unsupported session_mode: {session_mode}")
    q = """
    SELECT day
    FROM (
      SELECT DISTINCT toDate(toTimeZone(ts_start, 'America/New_York')) AS day
      FROM trading.bars_1m_ibkr
      WHERE symbol = %(s)s
        AND (%(session)s = 'extended' OR is_rth = 1)
        AND ts_start < %(start)s
      ORDER BY day DESC
      LIMIT %(n)s
    )
    ORDER BY day ASC
    """
    ch = get_client()
    rows = ch.query(
        q,
        {"s": symbol, "start": f"{start_date} 00:00:00", "n": int(warmup_trading_days), "session": session},
        settings={"use_query_cache": False},
    )
    if not rows:
        return start_date
    return str(rows[0].get("day") or start_date)


def load_option_quotes(
    start_date: str,
    end_date: str,
    underlying: str = "QQQ",
):
    if get_client is None:
        raise RuntimeError("ClickHouse client unavailable")
    q = """
    SELECT
      ts,
      expiry,
      strike,
      right,
      dte,
      bid,
      ask,
      mid,
      iv,
      delta,
      gamma,
      theta,
      vega,
      open_interest
    FROM trading.options_quotes_1m
    WHERE underlying = %(u)s
      AND ts >= %(start)s
      AND ts < %(end)s
      AND mid > 0
    ORDER BY ts
    """
    ch = get_client()
    rows = ch.query(
        q,
        {"u": underlying, "start": f"{start_date} 00:00:00", "end": f"{end_date} 00:00:00"},
        settings={"use_query_cache": False},
    )
    cache = {}
    for row in rows:
        ts_raw = pd.Timestamp(row["ts"])
        if ts_raw.tzinfo is None:
            ts_raw = ts_raw.tz_localize("UTC")
        ts = ts_raw.tz_convert("America/New_York").tz_localize(None).to_pydatetime().replace(second=0, microsecond=0)
        quote = ContractQuote(
            ts=ts,
            expiry=str(row.get("expiry")),
            right=str(row.get("right") or ""),
            strike=_safe_float(row.get("strike")),
            dte=_safe_int(row.get("dte")),
            bid=_safe_float(row.get("bid")),
            ask=_safe_float(row.get("ask")),
            mid=_safe_float(row.get("mid")),
            iv=_safe_float(row.get("iv")),
            delta=_safe_float(row.get("delta")),
            gamma=_safe_float(row.get("gamma")),
            theta=_safe_float(row.get("theta")),
            vega=_safe_float(row.get("vega")),
            open_interest=_safe_int(row.get("open_interest")),
        )
        cache.setdefault((ts, quote.right.upper()), []).append(quote)
    return cache


def synth_option_quotes_from_bars(
    bars: pd.DataFrame,
    *,
    underlying: str = "QQQ",
    strike_step: float | None = None,
    strike_wings: int = 4,
    base_iv_floor: float = 0.16,
    source: str = "synthetic_auto",
) -> Dict[Tuple[datetime, str], list[ContractQuote]]:
    if bars.empty:
        return {}
    df = bars.copy().sort_values("ts").reset_index(drop=True)
    df["ret_1m"] = df["close"].pct_change().fillna(0.0)
    df["rv_20m"] = df["ret_1m"].rolling(20, min_periods=10).std().fillna(0.0) * math.sqrt(390.0)
    df["abs_ret_5m"] = df["ret_1m"].abs().rolling(5, min_periods=3).sum().fillna(0.0)
    cache: Dict[Tuple[datetime, str], list[ContractQuote]] = {}

    for _, row in df.iterrows():
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        if ts.time() < time(9, 35) or ts.time() > time(15, 30):
            continue
        s = _safe_float(row.get("close"))
        if s <= 0:
            continue
        step = strike_step or _infer_strike_step(underlying, s)
        rv = _safe_float(row.get("rv_20m"))
        open_boost = _opening_iv_boost(ts.time(), _safe_float(row.get("abs_ret_5m")))
        iv_atm = max(base_iv_floor, 0.18 + 0.65 * rv + open_boost)
        atm = round(s / step) * step
        strikes = [round(atm + i * step, 2) for i in range(-strike_wings, strike_wings + 1)]
        expiries = [(ts.date(), 0), (_next_weekday(ts.date()), 1)]

        for expiry, dte in expiries:
            expiry_dt = datetime.combine(expiry, time(16, 0))
            t_year = max((expiry_dt - ts).total_seconds(), 60.0) / (365.0 * 24.0 * 3600.0)
            for strike in strikes:
                m = abs(strike / max(s, 1e-9) - 1.0)
                smile = 1.0 + 0.9 * (m ** 0.8)
                for right in ("C", "P"):
                    skew = 1.03 if (right == "P" and strike < s) else 1.0
                    term = 1.08 if dte == 0 else 1.0
                    iv = max(0.08, min(2.0, iv_atm * smile * skew * term))
                    mid, delta, gamma, theta, vega = _bs_price_greeks(s=s, k=float(strike), t_year=t_year, sigma=iv, right=right)
                    if mid <= 0:
                        continue
                    spread_pct = _synthetic_spread_pct(mid=mid, moneyness=m, dte=dte, bar_time=ts.time())
                    spread = mid * spread_pct
                    bid = max(0.01, mid - spread / 2.0)
                    ask = max(bid + 0.01, mid + spread / 2.0)
                    oi = _synthetic_open_interest(volume=_safe_float(row.get("volume")), moneyness=m, dte=dte)
                    quote = ContractQuote(
                        ts=ts,
                        expiry=str(expiry),
                        right=right,
                        strike=float(strike),
                        dte=int(dte),
                        bid=float(bid),
                        ask=float(ask),
                        mid=float(mid),
                        iv=float(iv),
                        delta=float(delta),
                        gamma=float(gamma),
                        theta=float(theta),
                        vega=float(vega),
                        open_interest=oi,
                    )
                    cache.setdefault((ts, right), []).append(quote)
    return cache


def load_option_quotes_for_replay(
    start_date: str,
    end_date: str,
    underlying: str,
    bars: pd.DataFrame,
    *,
    quote_mode: str = "real",
    backfill_mode: str = "none",
    backfill_csv_dir: str | None = None,
    backfill_source_tag: str = "replay_auto_backfill_v1",
) -> tuple[Dict[Tuple[datetime, str], list[ContractQuote]], Dict[str, Any]]:
    mode = quote_mode.strip().lower()
    if mode not in {"real", "synthetic", "auto"}:
        raise ValueError(f"unsupported quote_mode: {quote_mode}")
    fill_mode = backfill_mode.strip().lower()
    if fill_mode not in {"none", "synth", "csv"}:
        raise ValueError(f"unsupported backfill_mode: {backfill_mode}")
    if mode in {"real", "auto"}:
        real_cache = load_option_quotes(start_date, end_date, underlying=underlying)
        if real_cache:
            return real_cache, {
                "data_mode": "real",
                "confidence": "high",
                "source": "trading.options_quotes_1m",
                "backfill_attempted": False,
                "backfill_mode": fill_mode,
            }
        backfill_result = None
        if fill_mode != "none":
            backfill_result = attempt_quotes_backfill(
                underlying=underlying,
                start_date=start_date,
                end_date=end_date,
                backfill_mode=fill_mode,
                csv_dir=backfill_csv_dir,
                source_tag=backfill_source_tag,
            )
            if backfill_result.get("succeeded"):
                real_cache = load_option_quotes(start_date, end_date, underlying=underlying)
                if real_cache:
                    return real_cache, {
                        "data_mode": "real_backfilled",
                        "confidence": "high",
                        "source": "trading.options_quotes_1m",
                        "backfill_attempted": True,
                        "backfill_mode": fill_mode,
                        "backfill_result": backfill_result,
                    }
        if mode == "real":
            return {}, {
                "data_mode": "real_missing",
                "confidence": "none",
                "source": "trading.options_quotes_1m",
                "backfill_attempted": fill_mode != "none",
                "backfill_mode": fill_mode,
                "backfill_result": backfill_result,
            }
    synth_cache = synth_option_quotes_from_bars(bars, underlying=underlying)
    return synth_cache, {
        "data_mode": "synthetic",
        "confidence": "low",
        "source": "synthetic_from_underlying_bars",
        "synthetic_profile": "atm_focused_dte01_smile_v1",
        "backfill_attempted": fill_mode != "none",
        "backfill_mode": fill_mode,
        "backfill_result": backfill_result if mode in {"real", "auto"} else None,
    }


def attempt_quotes_backfill(
    *,
    underlying: str,
    start_date: str,
    end_date: str,
    backfill_mode: str,
    csv_dir: str | None,
    source_tag: str,
) -> Dict[str, Any]:
    if backfill_mode == "none":
        return {"attempted": False, "mode": "none", "succeeded": False}
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "options" / "detect_and_backfill_options_quotes_1m.py"),
        "--underlying",
        underlying.upper(),
        "--start",
        start_date,
        "--end",
        end_date,
        "--mode",
        backfill_mode,
        "--source-tag",
        source_tag,
    ]
    if backfill_mode == "csv":
        if not csv_dir:
            return {
                "attempted": True,
                "mode": backfill_mode,
                "succeeded": False,
                "reason": "csv_dir_required",
            }
        cmd.extend(["--csv-dir", csv_dir])
    completed = subprocess.run(
        cmd,
        cwd=os.fspath(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "attempted": True,
        "mode": backfill_mode,
        "succeeded": completed.returncode == 0,
        "returncode": int(completed.returncode),
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def build_features(df: pd.DataFrame, cfg: ReplayConfig | None = None) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    profile = resolve_indicator_profile(cfg)
    out = df.copy()
    out["date"] = out["ts"].dt.date
    out["minute_of_day"] = out["ts"].dt.hour * 60 + out["ts"].dt.minute
    price_col = profile["price_source"]
    price = out[price_col].astype(float)

    prev_close = out.groupby("symbol")[price_col].shift(1)
    tr = pd.concat(
        [
            (out["high"] - out["low"]).abs(),
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_14"] = tr.rolling(14, min_periods=14).mean()
    out["ma10"] = price.rolling(10, min_periods=10).mean()
    out["ma20"] = price.rolling(20, min_periods=20).mean()
    out["ma200"] = price.rolling(200, min_periods=200).mean()
    out["vol_ma20"] = out["volume"].rolling(20, min_periods=20).mean()
    out["ma10_slope"] = out["ma10"].diff()

    out["cum_pv_day"] = (price * out["volume"]).groupby(out["date"]).cumsum()
    # Use np.nan instead of pd.NA to avoid NAType->float cast issues across pandas versions.
    out["cum_vol_day"] = out["volume"].groupby(out["date"]).cumsum().replace(0, np.nan)
    out["vwap_day"] = (out["cum_pv_day"] / out["cum_vol_day"]).astype("float64")

    # Pandas 2.x + Python 3.13 can raise on pd.NA -> float casts.
    # Keep range as float and mask zero-range bars for ratio calculations.
    rng = (out["high"] - out["low"]).astype(float)
    rng_nonzero = rng.mask(rng == 0.0)
    body = (out["close"] - out["open"]).abs()
    out["body"] = body
    out["range"] = rng.fillna(0.0)
    out["lower_wick"] = (out[["open", "close"]].min(axis=1) - out["low"]).clip(lower=0.0)
    out["upper_wick"] = (out["high"] - out[["open", "close"]].max(axis=1)).clip(lower=0.0)
    out["close_pos"] = ((out["close"] - out["low"]) / rng_nonzero).fillna(0.5)

    bars5 = out.copy()
    bars5["bar5"] = bars5["ts"].dt.floor("5min")
    agg5 = (
        bars5.groupby(["date", "bar5"], as_index=False)
        .agg(
            close_5m=(price_col, "last"),
            ma10_5m_src=(price_col, "last"),
            volume_5m=("volume", "sum"),
        )
        .sort_values(["date", "bar5"])
    )
    agg5["ma10_5m"] = agg5.groupby("date")["ma10_5m_src"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    agg5["ma20_5m"] = agg5.groupby("date")["ma10_5m_src"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    agg5["ma200_5m"] = agg5.groupby("date")["ma10_5m_src"].transform(lambda s: s.rolling(200, min_periods=200).mean())
    agg5["ma10_5m_slope"] = agg5.groupby("date")["ma10_5m"].diff()
    out["bar5"] = out["ts"].dt.floor("5min")
    out = out.merge(
        agg5[["date", "bar5", "close_5m", "ma10_5m", "ma20_5m", "ma200_5m", "ma10_5m_slope"]],
        on=["date", "bar5"],
        how="left",
    )
    return out


def is_in_entry_window(ts: datetime, cfg: ReplayConfig) -> bool:
    t = ts.time()
    return _parse_hhmm(cfg.entry_start_am) <= t <= _parse_hhmm(cfg.entry_end_pm)


def _parse_hhmm(value: str):
    return datetime.strptime(value, "%H:%M").time()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _next_weekday(d: date) -> date:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price_greeks(s: float, k: float, t_year: float, sigma: float, right: str) -> tuple[float, float, float, float, float]:
    if s <= 0 or k <= 0 or t_year <= 0 or sigma <= 1e-8:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    sqrt_t = math.sqrt(t_year)
    d1 = (math.log(s / k) + 0.5 * sigma * sigma * t_year) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if right == "C":
        price = s * _norm_cdf(d1) - k * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta = -s * _norm_pdf(d1) * sigma / (2.0 * sqrt_t)
    else:
        price = k * _norm_cdf(-d2) - s * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        theta = -s * _norm_pdf(d1) * sigma / (2.0 * sqrt_t)
    gamma = _norm_pdf(d1) / (s * sigma * sqrt_t)
    vega = s * _norm_pdf(d1) * sqrt_t / 100.0
    return max(price, 0.01), delta, gamma, theta / 365.0, vega


def _infer_strike_step(underlying: str, spot: float) -> float:
    if underlying.upper() in {"QQQ", "SPY", "IWM"}:
        return 1.0
    if spot < 200:
        return 1.0
    if spot < 1000:
        return 2.5
    return 5.0


def _opening_iv_boost(bar_time: time, abs_ret_5m: float) -> float:
    if bar_time <= time(10, 0):
        return min(0.18, abs_ret_5m * 4.0)
    if bar_time <= time(11, 0):
        return min(0.10, abs_ret_5m * 2.0)
    return min(0.04, abs_ret_5m)


def _synthetic_spread_pct(*, mid: float, moneyness: float, dte: int, bar_time: time) -> float:
    base = max(0.04, 0.10 / max(mid, 0.75))
    moneyness_penalty = min(0.12, moneyness * 1.8)
    dte_penalty = 0.03 if dte == 0 else 0.0
    open_penalty = 0.04 if bar_time <= time(10, 0) else 0.0
    close_penalty = 0.02 if bar_time >= time(15, 0) else 0.0
    return min(0.40, base + moneyness_penalty + dte_penalty + open_penalty + close_penalty)


def _synthetic_open_interest(*, volume: float, moneyness: float, dte: int) -> int:
    liquidity = max(10.0, volume / 250.0)
    moneyness_decay = max(0.20, 1.0 - (moneyness * 4.0))
    dte_boost = 1.10 if dte == 1 else 0.85
    return int(max(10, liquidity * moneyness_decay * dte_boost))


__all__ = [
    "build_features",
    "is_in_entry_window",
    "load_option_quotes",
    "load_option_quotes_for_replay",
    "load_spy_bars",
    "synth_option_quotes_from_bars",
]
