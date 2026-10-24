#!/usr/bin/env python3
"""
Run a single LLM trading bot

Example usage:
    python run_single_bot.py --provider openai --symbols AAPL TSLA NVDA
    python run_single_bot.py --provider gemini --symbols AAPL MSFT --cycles 20
    python run_single_bot.py --provider deepseek_nvidia --symbols NVDA --live
"""

import asyncio
import sys
from pathlib import Path

# Enable nested event loops for ib_insync compatibility
import nest_asyncio
nest_asyncio.apply()

# Add project root directory to path (parent of llm_trader/)
sys.path.insert(0, str(Path(__file__).parent.parent))

from llm_trader.llm_trader_bot import LLMTraderBot
from llm_trader.utils.config import Config


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="Run LLM Trading Bot")
    parser.add_argument(
        "--provider",
        required=True,
        choices=["openai", "gemini", "deepseek", "deepseek_nvidia", "ollama"],
        help="LLM provider to use",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Symbols to trade (default: load from TRADING_SYMBOLS in .env)",
    )
    parser.add_argument(
        "--cash",
        type=float,
        default=50000.0,
        help="Initial cash",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between trading cycles",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Max number of cycles (default: run forever)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enable dry run mode (simulation only, no real orders)",
    )
    parser.add_argument(
        "--enable-v2",
        action="store_true",
        help="Enable V2 features (memory, time-awareness, adaptive frequency)",
    )
    parser.add_argument(
        "--enable-memory",
        action="store_true",
        help="Enable memory system (V2)",
    )
    parser.add_argument(
        "--enable-time-awareness",
        action="store_true",
        help="Enable time awareness (V2)",
    )
    parser.add_argument(
        "--enable-adaptive-frequency",
        action="store_true",
        help="Enable adaptive polling frequency (V2)",
    )
    parser.add_argument(
        "--use-aggregator",
        action="store_true",
        default=True,
        help="Use realtime in-memory aggregator from 1s bars (default: True)",
    )
    parser.add_argument(
        "--prefill-5s-mins",
        type=int,
        default=20,
        help="Cold start: prefill last N minutes of 5s bars from ClickHouse (default: 20)",
    )
    parser.add_argument(
        "--higher-tf-refresh-sec",
        type=int,
        default=300,
        help="Refresh 1h/1d background snapshot interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--close-at",
        type=str,
        default=None,
        help='Fixed EOD flatten time in ET, e.g. "15:55"',
    )
    parser.add_argument(
        "--close-before",
        type=int,
        default=5,
        help="Minutes before 16:00 ET to trigger flatten (default: 5 → 15:55)",
    )
    parser.add_argument(
        "--forbid-new-positions-before",
        type=int,
        default=5,
        help="Minutes before close to forbid opening new positions (default: 5)",
    )
    parser.add_argument(
        "--start-trading-at",
        type=str,
        default=None,
        help='Time to start allowing trades in ET, e.g. "9:40" (default: allow immediately)',
    )
    parser.add_argument(
        "--stop-trading-at",
        type=str,
        default=None,
        help='Time to stop new trades in ET, e.g. "10:30" (default: trade until close)',
    )
    parser.add_argument(
        "--heartbeat-file",
        type=str,
        default=None,
        help="Optional path to write heartbeat JSON periodically",
    )
    # V2.5: 缺数降级参数
    parser.add_argument(
        "--min-bars-1m",
        type=int,
        default=10,
        help="Minimum 1m bars required to allow new positions (default: 10)",
    )
    parser.add_argument(
        "--min-bars-5m",
        type=int,
        default=3,
        help="Minimum 5m bars required to allow new positions (default: 3)",
    )
    parser.add_argument(
        "--min-bars-15m",
        type=int,
        default=1,
        help="Minimum 15m bars required to allow new positions (default: 1)",
    )
    parser.add_argument(
        "--disable-degrade",
        action="store_true",
        help="Disable auto-degrade when data incomplete (default: enabled)",
    )
    parser.add_argument(
        "--wait-for-market",
        action="store_true",
        help="Wait until market opens (9:30 ET) before starting trading (default: start immediately)",
    )
    parser.add_argument(
        "--wait-for-premarket",
        action="store_true",
        help="Wait until premarket opens (4:00 ET) before starting trading (default: start immediately)",
    )
    parser.add_argument(
        "--wait-for-afterhours",
        action="store_true",
        help="Wait until after-hours trading opens (16:00 ET) before starting trading (default: start immediately)",
    )
    parser.add_argument(
        "--wait-for-overnight",
        action="store_true",
        help="Wait until overnight trading opens (20:00 ET) before starting trading (default: start immediately)",
    )
    # V2.6: Compact prompt system
    parser.add_argument(
        "--compact-prompt",
        action="store_true",
        help="Use compact prompt system for 83%% token reduction (high-frequency execution mode)",
    )
    parser.add_argument(
        "--reflection-interval",
        type=int,
        default=5,
        help="Minutes between reflection prompts when using compact mode (default: 5)",
    )
    # V2.2: Short selling
    parser.add_argument(
        "--allow-short-selling",
        action="store_true",
        help="Allow short selling (selling without position to open short) - default: disabled",
    )
    # V2.3: Session-based ledger system (enabled by default)
    parser.add_argument(
        "--disable-ledger",
        action="store_true",
        help="Disable ledger recording (ledger is enabled by default)",
    )
    parser.add_argument(
        "--session-name",
        default=None,
        help="Optional friendly session name/notes (e.g., 'TSLA-AggressiveStrategy')",
    )
    parser.add_argument(
        "--ledger-db",
        default="llm",
        help="ClickHouse database for LLM ledger (default: llm)",
    )

    args = parser.parse_args()

    # Load config
    config = Config()

    # Use config symbols if not provided via CLI
    if args.symbols is None:
        args.symbols = config.trading_symbols
        print(f"Using {len(args.symbols)} symbols from config: {args.symbols[:5]}{'...' if len(args.symbols) > 5 else ''}")

    # V2: If --enable-v2 flag is set, enable all V2 features
    if args.enable_v2:
        enable_memory = True
        enable_time_awareness = True
        enable_adaptive_frequency = True
    else:
        enable_memory = args.enable_memory
        enable_time_awareness = args.enable_time_awareness
        enable_adaptive_frequency = args.enable_adaptive_frequency

    # Determine dry_run mode
    # Default behavior is driven by TRADING_MODE environment variable
    trading_mode = os.getenv("TRADING_MODE", "shadow").lower()
    dry_run = True if trading_mode in ["shadow", "paper"] else False

    # --dry-run flag overrides TRADING_MODE
    if args.dry_run:
        dry_run = True

    # V2.3: Initialize ledger system (enabled by default)
    import os
    from llm_trader.ledger_manager import LedgerManager, NoopLedger

    if not args.disable_ledger:
        # V2.3: Use native protocol port 9000 for clickhouse_driver (not HTTP port 8123)
        ledger = LedgerManager(
            host=os.getenv("CLICKHOUSE_HOST", config.ch_host),
            port=int(os.getenv("CLICKHOUSE_NATIVE_PORT", "9000")),  # Native protocol port
            user=os.getenv("CLICKHOUSE_USER", config.ch_user),
            password=os.getenv("CLICKHOUSE_PASSWORD", config.ch_password),
            database=args.ledger_db,
        )
        ledger.ensure_tables()
        print(f"✓ Ledger system enabled (database: {args.ledger_db})")
    else:
        ledger = NoopLedger()
        print(f"⚠️  Ledger disabled (use default behavior to enable)")

    # Create session (even if ledger disabled, NoopLedger will return a session_id)
    session_id = ledger.create_session(
        bot_id=f"{args.provider.upper()}_{args.session_name or 'DEFAULT'}",
        model=args.provider,  # Will be updated with actual model name after provider init
        symbols=args.symbols,
        mode=trading_mode.upper(), # Use actual trading_mode from env
        initial_cash=float(args.cash),
        prompt_version_hash="v2.6_compact" if args.compact_prompt else "v2.5",
        provider_config_hash=f"{args.provider}_{args.interval}s",
        notes=args.session_name or f"Auto session {args.provider} {','.join(args.symbols[:3])}",
    )

    if not args.disable_ledger:
        print(f"✓ Session created: {session_id}")
        print(f"  Bot ID: {args.provider.upper()}_{args.session_name or 'DEFAULT'}")
        print(f"  Symbols: {', '.join(args.symbols)}")
        print(f"  Mode: {trading_mode.upper()}")
        print(f"  Initial Cash: ${args.cash:,.2f}")

    # Create bot
    bot = LLMTraderBot(
        provider_name=args.provider,
        symbols=args.symbols,
        config=config,
        initial_cash=args.cash,
        dry_run=dry_run,
        enable_memory=enable_memory,
        enable_time_awareness=enable_time_awareness,
        enable_adaptive_frequency=enable_adaptive_frequency,
        # Aggregator settings
        use_aggregator=args.use_aggregator,
        prefill_5s_mins=args.prefill_5s_mins,
        higher_tf_refresh_sec=args.higher_tf_refresh_sec,
        # Time management
        close_at=args.close_at,
        close_before=args.close_before,
        forbid_new_positions_before=args.forbid_new_positions_before,
        start_trading_at=args.start_trading_at,
        stop_trading_at=args.stop_trading_at,
        # V2.6: Compact prompt system
        use_compact_prompt=args.compact_prompt,
        reflection_interval_minutes=args.reflection_interval,
        # V2.2: Short selling
        allow_short_selling=args.allow_short_selling,
        # V2.3: Ledger system
        ledger=ledger,
        session_id=session_id,
    )

    # Run bot (V2.9: Support all trading sessions)
    asyncio.run(bot.run(
        interval=args.interval,
        max_cycles=args.cycles,
        wait_for_market=args.wait_for_market,
        wait_for_premarket=args.wait_for_premarket,
        wait_for_afterhours=args.wait_for_afterhours,
        wait_for_overnight=args.wait_for_overnight,
        heartbeat_file=args.heartbeat_file,
    ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⚠️ Bot interrupted by user")
        sys.exit(0)
    except Exception as e:
        import traceback
        print(f"\n❌ FATAL ERROR: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
