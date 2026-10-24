from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from libs.options.internal_symbol import format_internal_opt_symbol
from services.market.options_quote_store import query_option_rows
from services.option_replay.core.market_data import build_features
from services.option_replay.core.selector import explain_contract_rejection, select_contract
from services.option_replay.core.types import ContractQuote, ReplayConfig

ET = ZoneInfo("America/New_York")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def normalize_expiry_ymd(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw.replace("-", "")


def minute_floor(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def ensure_et_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=ET)
    return ts.astimezone(ET)


def build_replay_cfg_from_extras(extras: Dict[str, Any], spread_max_pct: Optional[float] = None) -> ReplayConfig:
    indicator_profile = str(
        extras.get("indicator_profile")
        or extras.get("market_data_profile_id")
        or "standard_close_extended_sma"
    ).strip() or "standard_close_extended_sma"
    session_mode = str(extras.get("market_data_session") or "").strip().lower()
    if not session_mode:
        session_mode = "extended" if "extended" in indicator_profile else "rth"
    return ReplayConfig.from_dict(
        {
            "market_data_session": session_mode,
            "indicator_profile": indicator_profile,
            "fixed_contracts_per_trade": int(extras.get("contracts", 4) or 4),
            "confirm_window_bars": int(extras.get("confirm_window_bars", 2) or 2),
            "confirm_decay_from_bar": int(extras.get("confirm_decay_from_bar", 99) or 99),
            "confirm_decay_max_distance_atr": float(extras.get("confirm_decay_max_distance_atr", 0.30) or 0.30),
            "enable_fresh_momentum_branch": bool(extras.get("enable_fresh_momentum_branch", True)),
            "enable_trend_continuation_branch": bool(extras.get("enable_trend_continuation_branch", True)),
            "continuation_pending_entry_timeout_bars": int(extras.get("continuation_pending_entry_timeout_bars", 2) or 2),
            "continuation_dynamic_reprice_to_ask": bool(extras.get("continuation_dynamic_reprice_to_ask", True)),
            "continuation_max_premium_expansion_pct": float(extras.get("continuation_max_premium_expansion_pct", 0.08) or 0.08),
            "trend_cont_put_consecutive_bars_below_ma10": int(extras.get("trend_cont_put_consecutive_bars_below_ma10", 2) or 2),
            "put_close_pos_max_for_entry": float(extras.get("put_close_pos_max_for_entry", 0.65) or 0.65),
            "trend_cont_put_max_high_over_ma10_atr": float(extras.get("trend_cont_put_max_high_over_ma10_atr", 0.2) or 0.2),
            "ma10_reject_ext_atr_max": float(extras.get("ma10_reject_ext_atr_max", 2.2) or 2.2),
            "enable_entry_quality_gate": False,
            "enable_daily_best_additive_branch": bool(extras.get("enable_daily_best_additive_branch", True)),
            "daily_best_call_only": bool(extras.get("daily_best_call_only", True)),
            "daily_best_min_dist_vwap_atr": float(extras.get("daily_best_min_dist_vwap_atr", 2.0) or 2.0),
            "delta_min": float(extras.get("delta_min", 0.45) or 0.45),
            "delta_max": float(extras.get("delta_max", 0.55) or 0.55),
            "delta_fallback_min": float(extras.get("delta_fallback_min", 0.40) or 0.40),
            "delta_fallback_max": float(extras.get("delta_fallback_max", 0.60) or 0.60),
            # 0DTE strategies must preserve explicit 0 here; using `or 1`
            # would incorrectly coerce 0 -> 1 and filter out same-day contracts.
            "dte_preferred": safe_int(extras.get("dte_preferred"), 1),
            "dte_fallback_max": safe_int(extras.get("dte_fallback_max"), 2),
            "min_open_interest": int(extras.get("min_open_interest", 100) or 100),
            "max_spread_pct": float(spread_max_pct if spread_max_pct is not None else extras.get("spread_max_pct", 0.12) or 0.12),
        }
    )


def build_feature_frame_from_history(
    underlying: str,
    history: Iterable[Dict[str, Any]],
    extras: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for bar in history:
        ts = bar.get("ts")
        if not isinstance(ts, datetime):
            continue
        rows.append(
            {
                "ts": ts.replace(tzinfo=None) if ts.tzinfo is None else ts.astimezone(ts.tzinfo).replace(tzinfo=None),
                "symbol": str(underlying),
                "open": safe_float(bar.get("open")),
                "high": safe_float(bar.get("high")),
                "low": safe_float(bar.get("low")),
                "close": safe_float(bar.get("close")),
                "vwap": safe_float(bar.get("vwap"), safe_float(bar.get("close"))),
                "volume": safe_float(bar.get("volume")),
            }
        )
    if not rows:
        return pd.DataFrame()
    cfg = build_replay_cfg_from_extras(dict(extras or {}))
    return build_features(pd.DataFrame(rows), cfg=cfg)


def select_option_quote_for_underlying(
    *,
    underlying: str,
    extras: Dict[str, Any],
    now: datetime,
    side: str,
    spot: float,
    spread_max_pct: float,
    now_for_quote_age: Optional[datetime] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    right = "C" if side == "CALL" else "P"
    now = ensure_et_aware(now)
    ts = minute_floor(now)
    lookback_min = max(1, int(extras.get("entry_quote_lookback_min", 2)))
    quote_max_age_sec = max(1.0, float(extras.get("entry_quote_max_age_sec", 90.0)))
    allow_stale_entry_quotes = bool(extras.get("allow_stale_entry_quotes", False))
    ts_lb = ts - timedelta(minutes=lookback_min)
    # Preserve explicit 0 for 0DTE contract scans.
    dte_min = max(0, safe_int(extras.get("dte_preferred"), 1))
    dte_max = max(dte_min, safe_int(extras.get("dte_fallback_max"), 2))
    allow_ch_fallback = bool(extras.get("entry_quote_allow_ch_fallback", False))
    rows = query_option_rows(
        underlying=underlying,
        right=right,
        dte_min=int(dte_min),
        dte_max=int(dte_max),
        ts_lb=ts_lb,
        ts_now=ts,
        fallback_to_ch=allow_ch_fallback,
    )
    if not rows:
        return None, "NO_QUOTES"

    ref_now = ensure_et_aware(now_for_quote_age) if isinstance(now_for_quote_age, datetime) else now
    stale_count = 0
    chain: List[ContractQuote] = []
    for row in rows:
        src = row.get("created_at") or row.get("ts")
        if not isinstance(src, datetime):
            stale_count += 1
            continue
        age_sec = max(0.0, (ref_now - ensure_et_aware(src)).total_seconds())
        if (not allow_stale_entry_quotes) and age_sec > quote_max_age_sec:
            stale_count += 1
            continue
        chain.append(
            ContractQuote(
                ts=minute_floor(ensure_et_aware(row.get("ts")) if isinstance(row.get("ts"), datetime) else now),
                expiry=str(row.get("expiry") or ""),
                right=str(row.get("right") or right),
                strike=safe_float(row.get("strike")),
                dte=int(row.get("dte") or 0),
                bid=safe_float(row.get("bid")),
                ask=safe_float(row.get("ask")),
                mid=safe_float(row.get("mid")),
                iv=safe_float(row.get("iv")),
                delta=safe_float(row.get("delta")),
                gamma=safe_float(row.get("gamma")),
                theta=safe_float(row.get("theta")),
                vega=safe_float(row.get("vega")),
                open_interest=int(row.get("open_interest") or 0),
            )
        )
    if not chain:
        return None, "STALE_QUOTE" if stale_count > 0 else "NO_VALID_QUOTE"

    cfg = build_replay_cfg_from_extras(extras, spread_max_pct=spread_max_pct)
    selected = select_contract(chain, right=right, spot=spot, aggressive_same_day=False, cfg=cfg)
    if selected is None:
        return None, explain_contract_rejection(chain, right=right, aggressive_same_day=False, cfg=cfg)

    expiry_ymd = normalize_expiry_ymd(selected.expiry)
    opt_symbol = format_internal_opt_symbol(
        underlying=underlying,
        expiry_ymd=expiry_ymd,
        strike=float(selected.strike),
        right=right,
    )
    quote_age_sec = float(
        min(
            (
                max(
                    0.0,
                    (ref_now - ensure_et_aware(row.get("created_at") or row.get("ts"))).total_seconds(),
                )
                for row in rows
                if str(row.get("expiry") or "") == str(selected.expiry)
                and str(row.get("right") or right) == str(selected.right)
                and abs(safe_float(row.get("strike")) - float(selected.strike)) <= 1e-9
                and isinstance((row.get("created_at") or row.get("ts")), datetime)
            ),
            default=0.0,
        )
    )
    quote_source = "unknown"
    best_age: Optional[float] = None
    for row in rows:
        if str(row.get("expiry") or "") != str(selected.expiry):
            continue
        if str(row.get("right") or right) != str(selected.right):
            continue
        if abs(safe_float(row.get("strike")) - float(selected.strike)) > 1e-9:
            continue
        src = row.get("created_at") or row.get("ts")
        if not isinstance(src, datetime):
            continue
        age = max(0.0, (ref_now - ensure_et_aware(src)).total_seconds())
        if best_age is None or age <= best_age:
            best_age = age
            quote_source = str(row.get("quote_source") or "unknown")
    return (
        {
            "opt_symbol": opt_symbol,
            "strike": float(selected.strike),
            "expiry": str(selected.expiry),
            "bid": float(selected.bid),
            "ask": float(selected.ask),
            "mid": float(selected.mid),
            "last": float(selected.mid),
            "quote_age_sec": quote_age_sec,
            "quote_source": quote_source,
        },
        None,
    )
