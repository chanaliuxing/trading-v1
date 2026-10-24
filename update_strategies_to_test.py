#!/usr/bin/env python3
"""
Update strategy configs in config.yml to use relaxed testing parameters
"""
import json
from pathlib import Path

# Read test configs
test_configs = {}
for test_config_path in Path("strategies/configs").glob("*_v7_2_test.json"):
    with open(test_config_path) as f:
        config = json.load(f)
        symbol = config['symbol']
        test_configs[symbol] = config

# Print what we'll apply
print("=" * 80)
print("RELAXED TESTING PARAMETERS")
print("=" * 80)
print()
print("Key parameter changes:")
print("  ✅ A1 VWAP threshold: 0.02 → 0.005 (4x more sensitive)")
print("  ✅ A1 RVOL range: [1.0-1.5] → [0.3-5.0] (much wider)")
print("  ✅ A2 Grid spacing: 0.6 → 0.3 (2x more signals)")
print("  ✅ A2 One-way filter: 2.5% → 15% (much less filtering)")
print("  ✅ B1 RVOL minimum: 1.1 → 0.5 (2x more sensitive)")
print("  ✅ Market regime: NEUTRAL (0.75x) → BULL (1.0x full size)")
print("  ✅ Pause thresholds: 10x higher (rarely pauses)")
print("  ✅ Drawdown limits: 2%/3% → 20%/50% (very lenient)")
print("  ✅ Cooldowns: 3-5 min → 1 min (faster re-entry)")
print("  ✅ All safety features disabled (calibration, anti-starvation, kill switch)")
print()
print("=" * 80)
print()
print(f"Loaded {len(test_configs)} test configs:")
for symbol in sorted(test_configs.keys()):
    print(f"  • {symbol}")
print()
print("To apply these to the running system:")
print("1. Stop the current system: pkill -f run_system.py")
print("2. These configs are ready in strategies/configs/*_v7_2_test.json")
print("3. Manually update config.yml to reference the _test.json files")
print("   OR update inline params with values from test configs")
print("4. Restart system: nohup venv/bin/python run_system.py > /tmp/trading_system.log 2>&1 &")
