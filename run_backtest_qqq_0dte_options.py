#!/usr/bin/env python3
"""
Backtest runner for QQQ 0DTE options strategy (backtest-only, synthetic pricing).
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

from libs.db.ch_pool import get_client
from strategies.qqq_0dte_options_strategy import QQQ0DTEOptionsStrategy
from libs.strategy import StrategyParams


def load_features(symbol: str, start: str, end: str) -> pd.DataFrame:
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
    FROM trading.bars_1m_ibkr
    WHERE symbol = %(sym)s
      AND ts_start >= %(start)s
      AND ts_start <= %(end)s
      AND is_rth = 1
    ORDER BY ts_start
    """
    ch = get_client()
    rows = ch.query(
        q,
        {
            "sym": symbol,
            "start": f"{start} 09:30:00",
            "end": f"{end} 16:00:00",
        },
        settings={"use_query_cache": False},
    )
    return pd.DataFrame(rows)


def compute_daily_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["ts"]).dt.date
    daily = df.groupby("date").agg(
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).reset_index()
    daily["prev_close"] = daily["close"].shift(1)
    daily["tr_hl"] = (daily["high"] - daily["low"]).abs()
    daily["tr_hpc"] = (daily["high"] - daily["prev_close"]).abs()
    daily["tr_lpc"] = (daily["low"] - daily["prev_close"]).abs()
    daily["tr"] = daily[["tr_hl", "tr_hpc", "tr_lpc"]].max(axis=1)
    daily["atr_d"] = daily["tr"].rolling(period, min_periods=1).mean()
    atr_map = dict(zip(daily["date"], daily["atr_d"]))
    df["atr_d"] = df["date"].map(atr_map).fillna(0.0)
    return df


def load_option_quotes_cache(underlying: str, start: str, end: str) -> dict:
    """Load 1m option quotes into {(ts_minute, right): [rows]} cache."""
    q = """
    SELECT
        ts,
        underlying,
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
        vega
    FROM trading.options_quotes_1m
    WHERE underlying = %(u)s
      AND ts >= %(start)s
      AND ts <= %(end)s
      AND dte IN (0, 1)
    ORDER BY ts
    """
    ch = get_client()
    rows = ch.query(
        q,
        {
            "u": underlying,
            "start": f"{start} 09:30:00",
            "end": f"{end} 16:00:00",
        },
        settings={"use_query_cache": False},
    )
    cache = {}
    for r in rows:
        ts = pd.Timestamp(r["ts"]).to_pydatetime()
        ts = ts.replace(second=0, microsecond=0)
        right = str(r["right"])
        key = (ts, right)
        cache.setdefault(key, []).append({
            "strike": float(r.get("strike") or 0.0),
            "mid": float(r.get("mid") or 0.0),
            "iv": float(r.get("iv") or 0.0),
            "delta": float(r.get("delta") or 0.0),
            "gamma": float(r.get("gamma") or 0.0),
            "theta": float(r.get("theta") or 0.0),
            "vega": float(r.get("vega") or 0.0),
            "dte": int(r.get("dte") or 0),
        })
    return cache


async def run_backtest(df: pd.DataFrame, params: dict) -> dict:
    # Minimal backtest loop (strategy-level PnL)
    params_obj = StrategyParams(extras=params)

    class _Ctx:
        def __init__(self):
            self.strategy_id = "qqq_0dte_bt"
            self.engine = None
            self.params = params_obj
            self.log = None
            self.metrics = None

    strat = QQQ0DTEOptionsStrategy(_Ctx(), params_obj)
    await strat.initialize()
    quotes_cache = params.get("_option_quotes_cache")
    if quotes_cache and hasattr(strat, "set_option_quotes_cache"):
        strat.set_option_quotes_cache(quotes_cache)

    cash = float(params.get("starting_cash", 100000.0))
    equity_curve = []

    for _, row in df.iterrows():
        feat = row.to_dict()
        await strat.on_features(str(feat.get("symbol")), feat)

        # Mark-to-market equity using synthetic premium if in position
        pos = strat._position  # pylint: disable=protected-access
        if pos is not None:
            premium, _, _ = strat._estimate_0dte_premium({
                "close": float(feat.get("close", 0.0)),
                "atr_1m": float(feat.get("atr_14", 0.0)),
                "atr_d": float(feat.get("atr_d", 0.0)),
            }, pos.side, feat["ts"].to_pydatetime(), params, strike=pos.strike)
            # Unrealized PnL in dollars
            unrealized = (premium - pos.entry_premium) * pos.qty * 100.0
        else:
            unrealized = 0.0

        equity_curve.append({
            "ts": int(pd.Timestamp(feat["ts"]).timestamp() * 1000),
            "equity": cash + unrealized,
        })

    trades = strat.get_trades()
    # Rough realized PnL from trades (entry/exit levels)
    realized = 0.0
    stack = []
    for t in trades:
        if t["event"] == "ENTRY":
            stack.append(dict(t))
        elif t["event"] in {"TP1", "EXIT"} and stack:
            entry = stack[0]
            qty = t["qty"]
            realized += (t["premium"] - entry["premium"]) * qty * 100.0
            entry["qty"] -= qty
            if entry["qty"] <= 0:
                stack.pop(0)

    return {
        "trades": trades,
        "realized_pnl": realized,
        "equity_curve": equity_curve,
    }

def main():
    parser = argparse.ArgumentParser(description="Backtest QQQ 0DTE options strategy")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Number of days (from today backwards)")
    parser.add_argument("--output", type=str, help="Output JSON file for results")
    parser.add_argument("--use-chain", action="store_true", help="Use options_quotes_1m as premium/greeks source when available")
    args = parser.parse_args()

    if args.days:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    elif args.start and args.end:
        start_date = args.start
        end_date = args.end
    else:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (now_ny() - timedelta(days=30)).strftime("%Y-%m-%d")

    print(f"Backtest QQQ 0DTE options: {start_date} to {end_date}")

    df = load_features("QQQ", start_date, end_date)
    if df.empty:
        raise SystemExit("[backtest] no data")

    df = compute_daily_atr(df, period=14)

    params = {
        "symbol": "QQQ",
        "contracts_per_trade": 3,
        "max_trades_per_day": 1,
        "ab_time_start": "09:30:00",
        "ab_time_end": "14:00:00",
        "delayed_confirm_end": "10:00:00",
        "delayed_confirm_move5m_min": 0.155,
        "early_override_window_start": "09:39:00",
        "early_override_window_end": "09:45:00",
        "early_override_move_mult": 0.16,
        "early_override_vol_min": 0.60,
        "momentum_deadzone": 0.05,
        "atr_rank_skip_max": 0.15,
        "rvol_min": 1.5,
        "rvol_window": 20,
        "atr_expansion_mult": 1.2,
        "atr_expansion_window": 20,
        "tp1_mult": 1.20,
        "tp2_mult": 1.60,
        "stop_loss_mult": 0.65,
        "no_move_minutes": 5.0,
        "no_move_atr_mult": 0.15,
        "vwap_exit_minutes": 7.0,
        "iv_proxy": 0.35,
        "strike_step": 1.0,
        "starting_cash": 100000.0,
    }
    if args.use_chain:
        params["_option_quotes_cache"] = load_option_quotes_cache("QQQ", start_date, end_date)

    import asyncio
    result = asyncio.run(run_backtest(df, params))
    print(f"Realized PnL: {result['realized_pnl']:.2f}")
    print(f"Trades: {len(result['trades'])}")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(result["trades"]).to_json(out, orient="records", indent=2, date_format="iso")
        print(f"Saved trades to {out}")


if __name__ == "__main__":
    main()
