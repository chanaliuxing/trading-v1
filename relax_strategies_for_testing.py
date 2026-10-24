#!/usr/bin/env python3
"""
Relax strategy parameters for paper trading testing
This makes strategies trigger more easily to test order generation
"""
import json
from pathlib import Path

# Parameters to relax for testing
TEST_PARAMS = {
    "A1": {
        "thr_sigma": 0.005,      # Lower threshold (easier to trigger)
        "rvol_lo": 0.3,          # Much lower RVOL requirement
        "rvol_hi": 5.0           # Higher RVOL range
    },
    "A2": {
        "grid_g": 0.3,           # Smaller grid spacing (more signals)
        "oneway_thr": 15.0       # Much higher (less filtering)
    },
    "B1": {
        "rvol_min": 0.5          # Lower RVOL requirement
    },
    "initial_state": {
        "market_regime": "BULL",
        "market_mult": 1.0       # Full position sizing
    },
    "regime": {
        "market_mult": {
            "BULL": 1.0,
            "NEUTRAL": 1.0,       # No reduction in neutral
            "BEAR": 1.0           # No reduction in bear
        }
    },
    "pause": {
        "theta_vix": 0.5,        # High VIX threshold (rarely pauses)
        "theta_spread": 0.01,    # High spread threshold
        "theta_atr": 10.0,       # High ATR threshold
        "t_pause_min": 1         # Short pause duration
    },
    "dd_limits": {
        "soft_pct": 0.2,         # 20% drawdown before reduction
        "hard_pct": 0.5,         # 50% drawdown before stop
        "soft_mult": 1.0,        # No reduction at soft limit
        "hard_mult": 1.0         # No reduction at hard limit
    },
    "timing": {
        "cooldown": {
            "B1": 1,             # 1 minute cooldown
            "A1": 1,
            "A2": 1
        }
    },
    # Disable restrictive features
    "premarket_calibration": {"enabled": False},
    "anti_starvation": {"enabled": False},
    "lkg": {"enabled": False},
    "l1_quality": {"enabled": False},
    "kill_switch": {"enabled": False},
    "qqq_filler": {"enabled": False}
}

def relax_config(config_data):
    """Apply relaxed testing parameters to config"""
    for key, value in TEST_PARAMS.items():
        if key in config_data:
            if isinstance(value, dict):
                config_data[key].update(value)
            else:
                config_data[key] = value
        else:
            config_data[key] = value

    # Update description
    config_data["description"] = f"{config_data.get('symbol', 'UNKNOWN')} TEST - Relaxed for Paper Trading Testing"
    config_data["asof"] = "2025-10-15"

    return config_data

def main():
    """Create relaxed test configs for all v7_2 strategies"""
    configs_dir = Path("strategies/configs")

    # Find all v7_2 configs (excluding relaxed and test versions)
    v7_2_configs = list(configs_dir.glob("*_v7_2.json"))

    print(f"Found {len(v7_2_configs)} v7_2 configs to relax")

    for config_path in v7_2_configs:
        # Load original config
        with open(config_path) as f:
            config = json.load(f)

        # Apply relaxed parameters
        test_config = relax_config(config.copy())

        # Save as _test version
        test_path = config_path.parent / config_path.name.replace("_v7_2.json", "_v7_2_test.json")

        with open(test_path, 'w') as f:
            json.dump(test_config, f, indent=2)

        print(f"✅ Created {test_path.name}")
        print(f"   A1 threshold: {test_config['A1']['thr_sigma']}")
        print(f"   A1 RVOL: {test_config['A1']['rvol_lo']}-{test_config['A1']['rvol_hi']}")
        print(f"   A2 grid_g: {test_config['A2']['grid_g']}")
        print(f"   A2 oneway: {test_config['A2']['oneway_thr']}")
        print(f"   B1 RVOL: {test_config['B1']['rvol_min']}")
        print(f"   Market mult: {test_config['regime']['market_mult']}")
        print()

if __name__ == "__main__":
    main()
