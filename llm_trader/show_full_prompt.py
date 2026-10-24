"""Show full prompt sent to LLM - for debugging"""

import asyncio
from llm_trader.data.market_data_fetcher import MarketDataFetcher
from llm_trader.utils.prompt_builder_r1 import R1PromptBuilder
from llm_trader.utils.time_awareness import TimeAwareness
from llm_trader.utils.memory_system import MemorySystem


async def main():
    print("=" * 100)
    print("FULL PROMPT DISPLAY - DeepSeek R1")
    print("=" * 100)
    print()

    # Initialize components
    fetcher = MarketDataFetcher()
    await fetcher.connect()

    # Fetch real market data for TSLA
    symbol = "TSLA"
    print(f"Fetching real-time market data for {symbol}...")
    market_data = await fetcher.get_comprehensive_market_data(symbol)

    # Show what data we have
    print("\n" + "=" * 100)
    print("MARKET DATA AVAILABLE")
    print("=" * 100)
    print(f"Keys: {list(market_data.keys())}")
    print(f"Current Price: ${market_data.get('current_price', 0):.2f}")
    print(f"5s Features: {len(market_data.get('features_5s', []))} bars")
    print(f"1s Bars: {len(market_data.get('bars_1s', []))} bars")

    sfs = market_data.get('sfs_features')
    if sfs:
        print(f"SFS Features: {len(sfs)} fields")
        print(f"  Available fields: {list(sfs.keys())}")
    else:
        print(f"SFS Features: None (table empty)")

    # Sample data
    if market_data.get('features_5s'):
        latest_5s = market_data['features_5s'][0]
        print("\nLatest 5s bar sample:")
        for k, v in latest_5s.items():
            print(f"  {k}: {v}")

    if market_data.get('bars_1s') and len(market_data['bars_1s']) > 0:
        latest_1s = market_data['bars_1s'][0]
        print("\nLatest 1s bar sample:")
        for k, v in latest_1s.items():
            print(f"  {k}: {v}")

    # Portfolio state (simulated)
    portfolio_state = {
        'cash': 50000.0,
        'total_equity': 50000.0,
        'total_pnl': 0.0,
        'total_pnl_pct': 0.0,
        'positions': {},  # Empty - no position
        'num_positions': 0,
        'max_position_size': 10000.0,
        'available_buying_power': 47500.0,
    }

    print("\n" + "=" * 100)
    print("PORTFOLIO STATE")
    print("=" * 100)
    for k, v in portfolio_state.items():
        print(f"{k}: {v}")

    # Initialize V2 modules
    ta = TimeAwareness()
    ms = MemorySystem()
    builder = R1PromptBuilder(ta, ms)

    # Build the prompt
    print("\n" + "=" * 100)
    print("BUILDING PROMPT...")
    print("=" * 100)

    decision_prompt = builder.build_decision_prompt(symbol, market_data, portfolio_state)

    # Display the FULL prompt
    print("\n" + "=" * 100)
    print("FULL PROMPT SENT TO DEEPSEEK R1")
    print("=" * 100)
    print()
    print(decision_prompt)
    print()
    print("=" * 100)
    print(f"PROMPT LENGTH: {len(decision_prompt)} characters")
    print(f"ESTIMATED TOKENS: ~{len(decision_prompt.split())} words")
    print("=" * 100)

    await fetcher.close()


if __name__ == "__main__":
    asyncio.run(main())
