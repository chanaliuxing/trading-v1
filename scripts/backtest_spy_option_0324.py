#!/usr/bin/env python3
"""Replay intraday option strategies on the isolated option replay engine.

This script is intentionally isolated from the shared backtest engines.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.option_replay.core import (
    ReplayConfig,
    SpyOptionReplayBacktester,
    get_strategy,
    infer_warmup_start_date,
    list_strategies,
    load_option_quotes_for_replay,
    load_spy_bars,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest option replay strategies")
    ap.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--strategy", default="ma10_reject_0324", choices=list_strategies())
    ap.add_argument("--quote-mode", default="real", choices=["real", "synthetic", "auto"])
    ap.add_argument("--warmup-trading-days", type=int, default=2, help="Preload prior trading days for indicator warmup")
    ap.add_argument("--backfill-missing-quotes", default="none", choices=["none", "synth", "csv"])
    ap.add_argument("--backfill-csv-dir", help="CSV directory for --backfill-missing-quotes csv mode")
    ap.add_argument("--backfill-source-tag", default="replay_auto_backfill_v1")
    ap.add_argument("--config-json", help="Optional config override JSON file")
    ap.add_argument("--out-dir", default="backtest_results/qqq_option_0324")
    args = ap.parse_args()

    overrides = {}
    if args.config_json:
        overrides = json.loads(Path(args.config_json).read_text(encoding="utf-8"))

    cfg = ReplayConfig.from_dict(overrides)
    warmup_start = infer_warmup_start_date(
        args.start,
        symbol=args.symbol,
        warmup_trading_days=max(0, args.warmup_trading_days),
        session_mode=cfg.market_data_session,
    )
    bars = load_spy_bars(
        warmup_start,
        args.end,
        symbol=args.symbol,
        session_mode=cfg.market_data_session,
        quality_mode=cfg.market_data_mode,
        max_price_deviation_pct=cfg.clean_data_max_price_deviation_pct,
        max_bar_range_pct=cfg.clean_data_max_bar_range_pct,
    )
    if bars.empty:
        raise SystemExit("[backtest] no bars")
    option_cache, data_info = load_option_quotes_for_replay(
        warmup_start,
        args.end,
        underlying=args.symbol,
        bars=bars,
        quote_mode=args.quote_mode,
        backfill_mode=args.backfill_missing_quotes,
        backfill_csv_dir=args.backfill_csv_dir,
        backfill_source_tag=args.backfill_source_tag,
    )
    if not option_cache:
        raise SystemExit("[backtest] no option quotes")

    strategy = get_strategy(args.strategy)
    bt = SpyOptionReplayBacktester(
        bars=bars,
        option_cache=option_cache,
        config=cfg,
        strategy=strategy,
        trade_start_date=args.start,
    )
    result = bt.run()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result["trades"].to_csv(out_dir / "trades.csv", index=False)
    result["skips"].to_csv(out_dir / "skips.csv", index=False)
    result["entry_audit"].to_csv(out_dir / "entry_audit.csv", index=False)
    result["selection_audit"].to_csv(out_dir / "selection_audit.csv", index=False)
    result["selection_candidates"].to_csv(out_dir / "selection_candidates.csv", index=False)
    result["bracket_audit"].to_csv(out_dir / "bracket_audit.csv", index=False)
    result["orders"].to_csv(out_dir / "orders.csv", index=False)
    result["fills"].to_csv(out_dir / "fills.csv", index=False)
    result["cash_ledger"].to_csv(out_dir / "cash_ledger.csv", index=False)
    result["equity_curve"].to_csv(out_dir / "equity_curve.csv", index=False)
    payload = {
        "summary": result["summary"],
        "config": result["config"],
        "notes": {
            "execution_model": "quote-based conservative fill model approximating IBKR adaptive/bracket behavior",
            "data": f"{args.symbol} bars from trading.bars_1m_ibkr",
            "quote_mode": args.quote_mode,
            "warmup_trading_days": args.warmup_trading_days,
            "warmup_start": warmup_start,
            "market_data_mode": cfg.market_data_mode,
            "market_data_session": cfg.market_data_session,
            "indicator_profile": cfg.indicator_profile,
            "data_mode": data_info["data_mode"],
            "confidence": data_info["confidence"],
            "quotes_source": data_info["source"],
            "synthetic_profile": data_info.get("synthetic_profile"),
            "backfill_mode": args.backfill_missing_quotes,
            "backfill_attempted": data_info.get("backfill_attempted", False),
            "backfill_result": data_info.get("backfill_result"),
            "strategy": args.strategy,
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    summary = result["summary"]
    print("=== Option Replay ===")
    print(f"Strategy: {args.strategy}")
    print(f"Range: {summary['start']} -> {summary['end']}")
    print(f"Trades: {summary['trades']}")
    print(f"Win rate: {summary['win_rate']:.2%}")
    print(f"Realized PnL: {summary['realized_pnl']:.2f}")
    print(f"Max drawdown: {summary['max_drawdown_pct']:.2%}")
    print(f"Skipped signals: {summary['skipped_signals']}")


if __name__ == "__main__":
    main()
