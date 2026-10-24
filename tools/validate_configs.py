#!/usr/bin/env python3
"""
Validate that all strategy configs are 100% complete with all required sections
"""
import json
from pathlib import Path

# Required top-level sections
REQUIRED_SECTIONS = [
    "symbol", "asof", "description", "windows", "target_triggers",
    "initial_state", "regime", "pause", "k_atr", "timing", "data_limits",
    "A1", "A2", "B1", "positioning", "slippage", "commission_per_share",
    "edge_floor", "dd_limits", "anti_starvation", "premarket_calibration",
    "lkg", "l1_quality", "kill_switch", "qqq_filler", "market_regime_symbols"
]

# Required sub-sections
REQUIRED_SUBSECTIONS = {
    "windows": ["short_days", "long_days", "baseline_days"],
    "initial_state": ["market_regime", "market_mult", "latest_vix", "prev_vix"],
    "regime": ["epsilon", "market_mult", "vix_symbol"],
    "pause": ["theta_vix", "theta_spread", "theta_atr", "t_pause_min"],
    "timing": ["recalibration_interval_min", "eod_close_time", "cooldown"],
    "data_limits": ["bars_1m", "bars_5m", "bars_5m_120", "premarket_1m", "premarket_5m"],
    "A1": ["thr_sigma", "rvol_lo", "rvol_hi", "time_exit_min", "exit"],
    "A2": ["grid_g", "grid_g_floor", "oneway_thr", "max_layers", "layer_ratios", "exit"],
    "B1": ["orb_len", "orb_len_extended", "rvol_min", "pullback_window_M", "extend_orb", "extend_threshold", "breakout", "exit", "stop"],
    "positioning": ["base_unit_pct", "anti_starvation_probe_pct", "cap_bucket", "cap_symbol", "max_concurrent", "filler_cap_ratio", "ndx_bucket"],
    "slippage": ["eps_p95", "min", "max"],
    "dd_limits": ["soft_pct", "hard_pct", "soft_mult", "hard_mult", "lookback_days", "fallback_soft", "fallback_hard"],
    "anti_starvation": ["enabled", "L1_min", "L2_min", "relax_sigma", "relax_g", "probe_ratio", "probe_size_mult", "revert_after_losses", "cutoff", "rvol_window_expand_pct", "rvol_window_shrink_pct"],
    "premarket_calibration": ["enabled", "use_config_values", "trigger_estimate_weights", "bounds", "forward_validation", "baseline_shrinkage", "cold_start_check"],
    "lkg": ["enabled", "file", "adjustment_factors"],
    "l1_quality": ["enabled", "window_seconds", "percentile_threshold", "lookback_days", "pause_duration_min", "metrics"],
    "kill_switch": ["enabled", "atr_mult_1m", "luld_halt", "l1_disconnect_seconds"],
    "qqq_filler": ["enabled", "no_trade_threshold_min", "cap_ratio"]
}

def validate_config(config_file):
    """Validate a single config file"""
    errors = []
    warnings = []

    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
    except Exception as e:
        return [f"❌ Failed to load JSON: {e}"], []

    symbol = config.get("symbol", "UNKNOWN")

    # Check top-level sections
    for section in REQUIRED_SECTIONS:
        if section not in config:
            errors.append(f"Missing section: {section}")

    # Check sub-sections
    for section, required_fields in REQUIRED_SUBSECTIONS.items():
        if section in config:
            for field in required_fields:
                if field not in config[section]:
                    errors.append(f"Missing {section}.{field}")
        else:
            # Already caught in top-level check
            pass

    # Verify critical parameter bounds are symbol-specific
    if "premarket_calibration" in config and "bounds" in config["premarket_calibration"]:
        bounds = config["premarket_calibration"]["bounds"]
        required_bounds = ["A1_thr_sigma", "A2_grid_g", "k_atr", "A1_rvol_lo", "A1_rvol_hi", "A1_time_exit_min", "pause_theta_spread", "B1_rvol_min"]
        for bound in required_bounds:
            if bound not in bounds:
                warnings.append(f"Missing calibration bound: {bound}")

    # Check NDX bucket configuration
    if "positioning" in config and "ndx_bucket" in config["positioning"]:
        ndx = config["positioning"]["ndx_bucket"]
        required_ndx = ["symbols", "loss_max_pct", "correlation_lookback_days", "correlation_scale_min"]
        for field in required_ndx:
            if field not in ndx:
                errors.append(f"Missing positioning.ndx_bucket.{field}")

    return errors, warnings

def main():
    """Validate all config files"""
    configs_dir = Path("strategies/configs")
    symbols = ["TSLA", "AAPL", "NVDA", "MSFT", "AMD", "META", "GOOGL", "QQQ", "AMZN"]

    all_valid = True
    results = {}

    print("=" * 80)
    print("STRATEGY CONFIG VALIDATION REPORT")
    print("=" * 80)
    print()

    for symbol in symbols:
        config_file = configs_dir / f"{symbol.lower()}_v7_2.json"

        if not config_file.exists():
            print(f"❌ {symbol}: Config file not found")
            all_valid = False
            continue

        errors, warnings = validate_config(config_file)
        results[symbol] = {"errors": errors, "warnings": warnings}

        if errors:
            print(f"❌ {symbol}: {len(errors)} errors, {len(warnings)} warnings")
            for error in errors:
                print(f"   - ERROR: {error}")
            for warning in warnings:
                print(f"   - WARNING: {warning}")
            all_valid = False
        elif warnings:
            print(f"⚠️  {symbol}: {len(warnings)} warnings")
            for warning in warnings:
                print(f"   - WARNING: {warning}")
        else:
            print(f"✅ {symbol}: Complete (all 25 sections present)")

    print()
    print("=" * 80)
    if all_valid:
        print("✅ ALL CONFIGS ARE 100% COMPLETE!")
        print("=" * 80)
        return 0
    else:
        print("❌ SOME CONFIGS ARE INCOMPLETE - See errors above")
        print("=" * 80)
        return 1

if __name__ == "__main__":
    exit(main())
