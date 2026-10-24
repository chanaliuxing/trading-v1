#!/usr/bin/env python3
"""
Hot-reload strategy parameters into running system via Redis state
This allows testing without restarting the system
"""
import asyncio
import json
from pathlib import Path
import redis
import sys

async def update_strategy_params():
    """Update strategy parameters in Redis for hot reload"""

    # Connect to Redis
    r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

    # Test params to inject
    test_overrides = {
        "A1": {
            "thr_sigma": 0.005,
            "rvol_lo": 0.3,
            "rvol_hi": 5.0
        },
        "A2": {
            "grid_g": 0.3,
            "oneway_thr": 15.0
        },
        "B1": {
            "rvol_min": 0.5
        },
        "market_mult": 1.0,
        "market_regime": "BULL",
        "pause": {
            "theta_vix": 0.5,
            "theta_spread": 0.01,
            "theta_atr": 10.0
        },
        "dd_limits": {
            "soft_pct": 0.2,
            "hard_pct": 0.5,
            "soft_mult": 1.0,
            "hard_mult": 1.0
        }
    }

    # Strategy IDs in the system
    strategy_ids = [
        "tsla_v7_2_001",
        "aapl_v7_2_001",
        "nvda_v7_2_001",
        "msft_v7_2_001",
        "amd_v7_2_001",
        "meta_v7_2_001",
        "amzn_v7_2_001",
        "googl_v7_2_001",
        "qqq_v7_2_001"
    ]

    print("=" * 80)
    print("HOT-RELOADING TEST PARAMETERS")
    print("=" * 80)
    print()

    for strategy_id in strategy_ids:
        # Get current state
        state_key = f"strategy:{strategy_id}:state"
        current_state = r.get(state_key)

        if current_state:
            state = json.loads(current_state)
        else:
            state = {}

        # Apply test overrides
        state.update(test_overrides)

        # Save back to Redis
        r.set(state_key, json.dumps(state))

        print(f"✅ Updated {strategy_id}")
        print(f"   Market regime: {state.get('market_regime', 'N/A')}")
        print(f"   Market mult: {state.get('market_mult', 'N/A')}")
        print(f"   A1 threshold: {state.get('A1', {}).get('thr_sigma', 'N/A')}")
        print(f"   A2 grid_g: {state.get('A2', {}).get('grid_g', 'N/A')}")
        print()

    print("=" * 80)
    print("✅ ALL STRATEGIES UPDATED")
    print("=" * 80)
    print()
    print("Changes will take effect on the NEXT feature callback (within ~5-10 seconds)")
    print("Monitor logs: tail -f /tmp/trading_system.log | grep -E '(signal|Order|Decision)'")

if __name__ == "__main__":
    asyncio.run(update_strategy_params())
