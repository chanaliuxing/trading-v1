#!/usr/bin/env python3
"""
Complete all strategy JSON configs with full parameter sets
Based on strategy documents and volatility profiles
"""
import json
from pathlib import Path

# Base template with all required sections
BASE_TEMPLATE = {
    "windows": {
        "short_days": 7,
        "long_days": 60,
        "baseline_days": 180
    },
    "initial_state": {
        "market_regime": "NEUTRAL",
        "market_mult": 0.75,
        "latest_vix": 18.0,
        "prev_vix": 18.0
    },
    "timing": {
        "recalibration_interval_min": 15,
        "eod_close_time": "15:57",
        "cooldown": {
            "B1": 5,
            "A1": 3,
            "A2": 3
        }
    },
    "data_limits": {
        "bars_1m": 240,
        "bars_5m": 240,
        "bars_5m_120": 120,
        "premarket_1m": 2730,
        "premarket_5m": 4680
    },
    "commission_per_share": 0.0035,
    "market_regime_symbols": ["SPY", "QQQ"]
}

# Symbol-specific configs based on volatility profiles
SYMBOL_CONFIGS = {
    "NVDA": {
        "description": "NVDA Intraday Long V7.2 - High Volatility AI/GPU",
        "target_triggers": 4,
        "regime": {"epsilon": 0.0055, "market_mult": {"BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5}, "vix_symbol": "VIX"},
        "pause": {"theta_vix": 0.13, "theta_spread": 0.0008, "theta_atr": 2.3, "t_pause_min": 15},
        "k_atr": 2.2,
        "A1": {"thr_sigma": 0.0110, "rvol_lo": 0.85, "rvol_hi": 1.90, "time_exit_min": 40, "exit": {"vwap_threshold_pct": 0.003, "profit_take_pct": 0.7, "eod_clean_time": "14:30"}},
        "A2": {"grid_g": 1.50, "grid_g_floor": 1.00, "oneway_thr": 2.5, "max_layers": 4, "layer_ratios": [0.25, 0.20, 0.15, 0.10], "exit": {"anchor_profit_pct": 0.6, "anchor_plus_offset": 0.5, "eod_grid_multiplier": 0.25, "eod_clean_time": "14:30", "stop_multiplier": 1.9}},
        "B1": {"orb_len": 30, "orb_len_extended": 45, "rvol_min": 0.85, "pullback_window_M": 6, "extend_orb": False, "extend_threshold": 0.35, "breakout": {"threshold_mult": 1.001, "reclaim_mult": 0.998}, "exit": {"rvol_threshold": 1.2, "profit_mult": 1.002, "profit_ratios": [0.40, 0.40, 0.20], "profit_targets_R": [1.0, 2.0], "time_exit_orb30": "11:05", "time_exit_orb45": "11:20", "expansion_threshold": 2.0}, "stop": {"min_percentile": 25, "min_atr_mult": 0.40}},
        "positioning": {"base_unit_pct": 0.008, "anti_starvation_probe_pct": 0.25, "cap_bucket": 0.25, "cap_symbol": 0.10, "max_concurrent": 2, "filler_cap_ratio": 0.5, "ndx_bucket": {"symbols": ["QQQ", "NVDA", "AAPL", "MSFT", "META", "AMD", "AVGO", "GOOGL", "TSLA"], "loss_max_pct": 0.015, "correlation_lookback_days": 5, "correlation_scale_min": 0.25}},
        "slippage": {"eps_p95": 0.0006, "min": 0.0002, "max": 0.0010},
        "edge_floor": 0.20,
        "dd_limits": {"soft_pct": 0.020, "hard_pct": 0.032, "soft_mult": 0.5, "hard_mult": 0.0, "lookback_days": 60, "fallback_soft": 0.020, "fallback_hard": 0.030},
        "premarket_calibration": {"enabled": True, "use_config_values": True, "trigger_estimate_weights": {"a1": 0.1, "a2": 0.05}, "bounds": {"A1_thr_sigma": {"min": 0.006, "max": 0.025}, "A2_grid_g": {"min": 0.8, "max": 4.0}, "k_atr": {"min": 1.6, "max": 3.0}, "A1_rvol_lo": {"min": 0.75, "max": 1.10}, "A1_rvol_hi": {"min": 1.3, "max": 2.5}, "A1_time_exit_min": {"min": 25, "max": 60}, "pause_theta_spread": {"min": 0.0002, "max": 0.0015}, "B1_rvol_min": {"min": 0.75, "max": 1.10}}, "forward_validation": {"days": 30, "top_k": 3, "score_alpha": 1.0, "score_beta": 1.0}, "baseline_shrinkage": {"enabled": True, "min_weight": 0.5, "max_weight": 0.9}, "cold_start_check": {"enabled": True, "duration_min": 1, "edge_coverage_threshold": 0.70, "trigger_rate_threshold": 0.50}}
    },
    "MSFT": {
        "description": "MSFT Intraday Long V7.2 - Mega-cap Cloud/Enterprise",
        "target_triggers": 5,
        "regime": {"epsilon": 0.0042, "market_mult": {"BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5}, "vix_symbol": "VIX"},
        "pause": {"theta_vix": 0.11, "theta_spread": 0.0004, "theta_atr": 1.90, "t_pause_min": 15},
        "k_atr": 1.75,
        "A1": {"thr_sigma": 0.0042, "rvol_lo": 0.85, "rvol_hi": 1.75, "time_exit_min": 45, "exit": {"vwap_threshold_pct": 0.003, "profit_take_pct": 0.7, "eod_clean_time": "14:30"}},
        "A2": {"grid_g": 0.80, "grid_g_floor": 0.55, "oneway_thr": 2.2, "max_layers": 4, "layer_ratios": [0.25, 0.20, 0.15, 0.10], "exit": {"anchor_profit_pct": 0.6, "anchor_plus_offset": 0.5, "eod_grid_multiplier": 0.25, "eod_clean_time": "14:30", "stop_multiplier": 1.6}},
        "B1": {"orb_len": 30, "orb_len_extended": 45, "rvol_min": 0.85, "pullback_window_M": 6, "extend_orb": False, "extend_threshold": 0.35, "breakout": {"threshold_mult": 1.0008, "reclaim_mult": 0.998}, "exit": {"rvol_threshold": 1.2, "profit_mult": 1.002, "profit_ratios": [0.40, 0.40, 0.20], "profit_targets_R": [1.0, 2.0], "time_exit_orb30": "11:05", "time_exit_orb45": "11:20", "expansion_threshold": 2.0}, "stop": {"min_percentile": 25, "min_atr_mult": 0.30}},
        "positioning": {"base_unit_pct": 0.011, "anti_starvation_probe_pct": 0.25, "cap_bucket": 0.25, "cap_symbol": 0.10, "max_concurrent": 2, "filler_cap_ratio": 0.5, "ndx_bucket": {"symbols": ["QQQ", "NVDA", "AAPL", "MSFT", "META", "AMD", "AVGO", "GOOGL", "TSLA"], "loss_max_pct": 0.015, "correlation_lookback_days": 5, "correlation_scale_min": 0.25}},
        "slippage": {"eps_p95": 0.0003, "min": 0.0001, "max": 0.0005},
        "edge_floor": 0.15,
        "dd_limits": {"soft_pct": 0.014, "hard_pct": 0.023, "soft_mult": 0.5, "hard_mult": 0.0, "lookback_days": 60, "fallback_soft": 0.015, "fallback_hard": 0.025},
        "premarket_calibration": {"enabled": True, "use_config_values": True, "trigger_estimate_weights": {"a1": 0.1, "a2": 0.05}, "bounds": {"A1_thr_sigma": {"min": 0.0015, "max": 0.0075}, "A2_grid_g": {"min": 0.10, "max": 0.55}, "k_atr": {"min": 1.2, "max": 2.3}, "A1_rvol_lo": {"min": 0.85, "max": 0.95}, "A1_rvol_hi": {"min": 1.6, "max": 2.0}, "A1_time_exit_min": {"min": 20, "max": 50}, "pause_theta_spread": {"min": 0.0001, "max": 0.0006}, "B1_rvol_min": {"min": 0.80, "max": 0.90}}, "forward_validation": {"days": 30, "top_k": 3, "score_alpha": 1.0, "score_beta": 1.0}, "baseline_shrinkage": {"enabled": True, "min_weight": 0.5, "max_weight": 0.9}, "cold_start_check": {"enabled": True, "duration_min": 1, "edge_coverage_threshold": 0.70, "trigger_rate_threshold": 0.50}}
    },
    "AMD": {
        "description": "AMD Intraday Long V7.2 - Very High Volatility Semiconductor",
        "target_triggers": 4,
        "regime": {"epsilon": 0.0060, "market_mult": {"BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5}, "vix_symbol": "VIX"},
        "pause": {"theta_vix": 0.14, "theta_spread": 0.0009, "theta_atr": 2.4, "t_pause_min": 15},
        "k_atr": 2.4,
        "A1": {"thr_sigma": 0.0125, "rvol_lo": 0.85, "rvol_hi": 1.95, "time_exit_min": 38, "exit": {"vwap_threshold_pct": 0.003, "profit_take_pct": 0.7, "eod_clean_time": "14:30"}},
        "A2": {"grid_g": 1.70, "grid_g_floor": 1.10, "oneway_thr": 2.6, "max_layers": 4, "layer_ratios": [0.25, 0.20, 0.15, 0.10], "exit": {"anchor_profit_pct": 0.6, "anchor_plus_offset": 0.5, "eod_grid_multiplier": 0.25, "eod_clean_time": "14:30", "stop_multiplier": 2.0}},
        "B1": {"orb_len": 30, "orb_len_extended": 45, "rvol_min": 0.85, "pullback_window_M": 6, "extend_orb": False, "extend_threshold": 0.35, "breakout": {"threshold_mult": 1.001, "reclaim_mult": 0.998}, "exit": {"rvol_threshold": 1.2, "profit_mult": 1.002, "profit_ratios": [0.40, 0.40, 0.20], "profit_targets_R": [1.0, 2.0], "time_exit_orb30": "11:05", "time_exit_orb45": "11:20", "expansion_threshold": 2.0}, "stop": {"min_percentile": 25, "min_atr_mult": 0.42}},
        "positioning": {"base_unit_pct": 0.007, "anti_starvation_probe_pct": 0.25, "cap_bucket": 0.25, "cap_symbol": 0.10, "max_concurrent": 2, "filler_cap_ratio": 0.5, "ndx_bucket": {"symbols": ["QQQ", "NVDA", "AAPL", "MSFT", "META", "AMD", "AVGO", "GOOGL", "TSLA"], "loss_max_pct": 0.015, "correlation_lookback_days": 5, "correlation_scale_min": 0.25}},
        "slippage": {"eps_p95": 0.0007, "min": 0.0002, "max": 0.0012},
        "edge_floor": 0.22,
        "dd_limits": {"soft_pct": 0.022, "hard_pct": 0.035, "soft_mult": 0.5, "hard_mult": 0.0, "lookback_days": 60, "fallback_soft": 0.022, "fallback_hard": 0.032},
        "premarket_calibration": {"enabled": True, "use_config_values": True, "trigger_estimate_weights": {"a1": 0.1, "a2": 0.05}, "bounds": {"A1_thr_sigma": {"min": 0.007, "max": 0.028}, "A2_grid_g": {"min": 0.9, "max": 4.5}, "k_atr": {"min": 1.8, "max": 3.2}, "A1_rvol_lo": {"min": 0.75, "max": 1.10}, "A1_rvol_hi": {"min": 1.4, "max": 2.6}, "A1_time_exit_min": {"min": 25, "max": 55}, "pause_theta_spread": {"min": 0.0003, "max": 0.0018}, "B1_rvol_min": {"min": 0.75, "max": 1.10}}, "forward_validation": {"days": 30, "top_k": 3, "score_alpha": 1.0, "score_beta": 1.0}, "baseline_shrinkage": {"enabled": True, "min_weight": 0.5, "max_weight": 0.9}, "cold_start_check": {"enabled": True, "duration_min": 1, "edge_coverage_threshold": 0.70, "trigger_rate_threshold": 0.50}}
    },
    "META": {
        "description": "META Intraday Long V7.2 - Social Media/Ads",
        "target_triggers": 4,
        "regime": {"epsilon": 0.0050, "market_mult": {"BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5}, "vix_symbol": "VIX"},
        "pause": {"theta_vix": 0.12, "theta_spread": 0.0006, "theta_atr": 2.05, "t_pause_min": 15},
        "k_atr": 1.95,
        "A1": {"thr_sigma": 0.0085, "rvol_lo": 0.85, "rvol_hi": 1.85, "time_exit_min": 42, "exit": {"vwap_threshold_pct": 0.003, "profit_take_pct": 0.7, "eod_clean_time": "14:30"}},
        "A2": {"grid_g": 1.10, "grid_g_floor": 0.75, "oneway_thr": 2.3, "max_layers": 4, "layer_ratios": [0.25, 0.20, 0.15, 0.10], "exit": {"anchor_profit_pct": 0.6, "anchor_plus_offset": 0.5, "eod_grid_multiplier": 0.25, "eod_clean_time": "14:30", "stop_multiplier": 1.7}},
        "B1": {"orb_len": 30, "orb_len_extended": 45, "rvol_min": 0.85, "pullback_window_M": 6, "extend_orb": False, "extend_threshold": 0.35, "breakout": {"threshold_mult": 1.001, "reclaim_mult": 0.998}, "exit": {"rvol_threshold": 1.2, "profit_mult": 1.002, "profit_ratios": [0.40, 0.40, 0.20], "profit_targets_R": [1.0, 2.0], "time_exit_orb30": "11:05", "time_exit_orb45": "11:20", "expansion_threshold": 2.0}, "stop": {"min_percentile": 25, "min_atr_mult": 0.35}},
        "positioning": {"base_unit_pct": 0.009, "anti_starvation_probe_pct": 0.25, "cap_bucket": 0.25, "cap_symbol": 0.10, "max_concurrent": 2, "filler_cap_ratio": 0.5, "ndx_bucket": {"symbols": ["QQQ", "NVDA", "AAPL", "MSFT", "META", "AMD", "AVGO", "GOOGL", "TSLA"], "loss_max_pct": 0.015, "correlation_lookback_days": 5, "correlation_scale_min": 0.25}},
        "slippage": {"eps_p95": 0.0005, "min": 0.0002, "max": 0.0008},
        "edge_floor": 0.18,
        "dd_limits": {"soft_pct": 0.018, "hard_pct": 0.029, "soft_mult": 0.5, "hard_mult": 0.0, "lookback_days": 60, "fallback_soft": 0.018, "fallback_hard": 0.028},
        "premarket_calibration": {"enabled": True, "use_config_values": True, "trigger_estimate_weights": {"a1": 0.1, "a2": 0.05}, "bounds": {"A1_thr_sigma": {"min": 0.004, "max": 0.018}, "A2_grid_g": {"min": 0.5, "max": 2.5}, "k_atr": {"min": 1.4, "max": 2.8}, "A1_rvol_lo": {"min": 0.75, "max": 1.10}, "A1_rvol_hi": {"min": 1.2, "max": 2.3}, "A1_time_exit_min": {"min": 25, "max": 58}, "pause_theta_spread": {"min": 0.0002, "max": 0.0012}, "B1_rvol_min": {"min": 0.75, "max": 1.05}}, "forward_validation": {"days": 30, "top_k": 3, "score_alpha": 1.0, "score_beta": 1.0}, "baseline_shrinkage": {"enabled": True, "min_weight": 0.5, "max_weight": 0.9}, "cold_start_check": {"enabled": True, "duration_min": 1, "edge_coverage_threshold": 0.70, "trigger_rate_threshold": 0.50}}
    },
    "GOOGL": {
        "description": "GOOGL Intraday Long V7.2 - Mega-cap Search/Cloud",
        "target_triggers": 5,
        "regime": {"epsilon": 0.0044, "market_mult": {"BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5}, "vix_symbol": "VIX"},
        "pause": {"theta_vix": 0.11, "theta_spread": 0.0004, "theta_atr": 1.92, "t_pause_min": 15},
        "k_atr": 1.77,
        "A1": {"thr_sigma": 0.0044, "rvol_lo": 0.85, "rvol_hi": 1.75, "time_exit_min": 44, "exit": {"vwap_threshold_pct": 0.003, "profit_take_pct": 0.7, "eod_clean_time": "14:30"}},
        "A2": {"grid_g": 0.82, "grid_g_floor": 0.58, "oneway_thr": 2.2, "max_layers": 4, "layer_ratios": [0.25, 0.20, 0.15, 0.10], "exit": {"anchor_profit_pct": 0.6, "anchor_plus_offset": 0.5, "eod_grid_multiplier": 0.25, "eod_clean_time": "14:30", "stop_multiplier": 1.6}},
        "B1": {"orb_len": 30, "orb_len_extended": 45, "rvol_min": 0.85, "pullback_window_M": 6, "extend_orb": False, "extend_threshold": 0.35, "breakout": {"threshold_mult": 1.0008, "reclaim_mult": 0.998}, "exit": {"rvol_threshold": 1.2, "profit_mult": 1.002, "profit_ratios": [0.40, 0.40, 0.20], "profit_targets_R": [1.0, 2.0], "time_exit_orb30": "11:05", "time_exit_orb45": "11:20", "expansion_threshold": 2.0}, "stop": {"min_percentile": 25, "min_atr_mult": 0.30}},
        "positioning": {"base_unit_pct": 0.010, "anti_starvation_probe_pct": 0.25, "cap_bucket": 0.25, "cap_symbol": 0.10, "max_concurrent": 2, "filler_cap_ratio": 0.5, "ndx_bucket": {"symbols": ["QQQ", "NVDA", "AAPL", "MSFT", "META", "AMD", "AVGO", "GOOGL", "TSLA"], "loss_max_pct": 0.015, "correlation_lookback_days": 5, "correlation_scale_min": 0.25}},
        "slippage": {"eps_p95": 0.0003, "min": 0.0001, "max": 0.0005},
        "edge_floor": 0.15,
        "dd_limits": {"soft_pct": 0.015, "hard_pct": 0.024, "soft_mult": 0.5, "hard_mult": 0.0, "lookback_days": 60, "fallback_soft": 0.015, "fallback_hard": 0.025},
        "premarket_calibration": {"enabled": True, "use_config_values": True, "trigger_estimate_weights": {"a1": 0.1, "a2": 0.05}, "bounds": {"A1_thr_sigma": {"min": 0.0015, "max": 0.0078}, "A2_grid_g": {"min": 0.10, "max": 0.58}, "k_atr": {"min": 1.2, "max": 2.4}, "A1_rvol_lo": {"min": 0.85, "max": 0.95}, "A1_rvol_hi": {"min": 1.6, "max": 2.0}, "A1_time_exit_min": {"min": 20, "max": 50}, "pause_theta_spread": {"min": 0.0001, "max": 0.0006}, "B1_rvol_min": {"min": 0.80, "max": 0.90}}, "forward_validation": {"days": 30, "top_k": 3, "score_alpha": 1.0, "score_beta": 1.0}, "baseline_shrinkage": {"enabled": True, "min_weight": 0.5, "max_weight": 0.9}, "cold_start_check": {"enabled": True, "duration_min": 1, "edge_coverage_threshold": 0.70, "trigger_rate_threshold": 0.50}}
    },
    "QQQ": {
        "description": "QQQ Intraday Long V7.2 - Nasdaq 100 ETF (Lowest Volatility)",
        "target_triggers": 5,
        "regime": {"epsilon": 0.0035, "market_mult": {"BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5}, "vix_symbol": "VIX"},
        "pause": {"theta_vix": 0.10, "theta_spread": 0.0003, "theta_atr": 1.75, "t_pause_min": 15},
        "k_atr": 1.60,
        "A1": {"thr_sigma": 0.0035, "rvol_lo": 0.85, "rvol_hi": 1.70, "time_exit_min": 48, "exit": {"vwap_threshold_pct": 0.002, "profit_take_pct": 0.7, "eod_clean_time": "14:30"}},
        "A2": {"grid_g": 0.65, "grid_g_floor": 0.45, "oneway_thr": 2.0, "max_layers": 4, "layer_ratios": [0.25, 0.20, 0.15, 0.10], "exit": {"anchor_profit_pct": 0.6, "anchor_plus_offset": 0.5, "eod_grid_multiplier": 0.25, "eod_clean_time": "14:30", "stop_multiplier": 1.5}},
        "B1": {"orb_len": 30, "orb_len_extended": 45, "rvol_min": 0.85, "pullback_window_M": 6, "extend_orb": False, "extend_threshold": 0.35, "breakout": {"threshold_mult": 1.0006, "reclaim_mult": 0.9994}, "exit": {"rvol_threshold": 1.2, "profit_mult": 1.0015, "profit_ratios": [0.40, 0.40, 0.20], "profit_targets_R": [1.0, 2.0], "time_exit_orb30": "11:05", "time_exit_orb45": "11:20", "expansion_threshold": 2.0}, "stop": {"min_percentile": 25, "min_atr_mult": 0.28}},
        "positioning": {"base_unit_pct": 0.012, "anti_starvation_probe_pct": 0.25, "cap_bucket": 0.25, "cap_symbol": 0.10, "max_concurrent": 2, "filler_cap_ratio": 0.5, "ndx_bucket": {"symbols": ["QQQ", "NVDA", "AAPL", "MSFT", "META", "AMD", "AVGO", "GOOGL", "TSLA"], "loss_max_pct": 0.015, "correlation_lookback_days": 5, "correlation_scale_min": 0.25}},
        "slippage": {"eps_p95": 0.0002, "min": 0.0001, "max": 0.0004},
        "edge_floor": 0.12,
        "dd_limits": {"soft_pct": 0.012, "hard_pct": 0.020, "soft_mult": 0.5, "hard_mult": 0.0, "lookback_days": 60, "fallback_soft": 0.012, "fallback_hard": 0.020},
        "premarket_calibration": {"enabled": True, "use_config_values": True, "trigger_estimate_weights": {"a1": 0.1, "a2": 0.05}, "bounds": {"A1_thr_sigma": {"min": 0.0010, "max": 0.0065}, "A2_grid_g": {"min": 0.08, "max": 0.45}, "k_atr": {"min": 1.1, "max": 2.2}, "A1_rvol_lo": {"min": 0.85, "max": 0.95}, "A1_rvol_hi": {"min": 1.5, "max": 1.9}, "A1_time_exit_min": {"min": 25, "max": 55}, "pause_theta_spread": {"min": 0.0001, "max": 0.0005}, "B1_rvol_min": {"min": 0.80, "max": 0.90}}, "forward_validation": {"days": 30, "top_k": 3, "score_alpha": 1.0, "score_beta": 1.0}, "baseline_shrinkage": {"enabled": True, "min_weight": 0.5, "max_weight": 0.9}, "cold_start_check": {"enabled": True, "duration_min": 1, "edge_coverage_threshold": 0.70, "trigger_rate_threshold": 0.50}}
    },
    "AMZN": {
        "description": "AMZN Intraday Long V7.2 - Mega-cap E-commerce/Cloud",
        "target_triggers": 5,
        "regime": {"epsilon": 0.0048, "market_mult": {"BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5}, "vix_symbol": "VIX"},
        "pause": {"theta_vix": 0.12, "theta_spread": 0.0005, "theta_atr": 2.00, "t_pause_min": 15},
        "k_atr": 1.88,
        "A1": {"thr_sigma": 0.0048, "rvol_lo": 0.85, "rvol_hi": 1.78, "time_exit_min": 43, "exit": {"vwap_threshold_pct": 0.003, "profit_take_pct": 0.7, "eod_clean_time": "14:30"}},
        "A2": {"grid_g": 0.90, "grid_g_floor": 0.62, "oneway_thr": 2.2, "max_layers": 4, "layer_ratios": [0.25, 0.20, 0.15, 0.10], "exit": {"anchor_profit_pct": 0.6, "anchor_plus_offset": 0.5, "eod_grid_multiplier": 0.25, "eod_clean_time": "14:30", "stop_multiplier": 1.65}},
        "B1": {"orb_len": 30, "orb_len_extended": 45, "rvol_min": 0.85, "pullback_window_M": 6, "extend_orb": False, "extend_threshold": 0.35, "breakout": {"threshold_mult": 1.0008, "reclaim_mult": 0.998}, "exit": {"rvol_threshold": 1.2, "profit_mult": 1.002, "profit_ratios": [0.40, 0.40, 0.20], "profit_targets_R": [1.0, 2.0], "time_exit_orb30": "11:05", "time_exit_orb45": "11:20", "expansion_threshold": 2.0}, "stop": {"min_percentile": 25, "min_atr_mult": 0.32}},
        "positioning": {"base_unit_pct": 0.010, "anti_starvation_probe_pct": 0.25, "cap_bucket": 0.25, "cap_symbol": 0.10, "max_concurrent": 2, "filler_cap_ratio": 0.5, "ndx_bucket": {"symbols": ["QQQ", "NVDA", "AAPL", "MSFT", "META", "AMD", "AVGO", "GOOGL", "TSLA"], "loss_max_pct": 0.015, "correlation_lookback_days": 5, "correlation_scale_min": 0.25}},
        "slippage": {"eps_p95": 0.0004, "min": 0.0001, "max": 0.0006},
        "edge_floor": 0.16,
        "dd_limits": {"soft_pct": 0.016, "hard_pct": 0.026, "soft_mult": 0.5, "hard_mult": 0.0, "lookback_days": 60, "fallback_soft": 0.016, "fallback_hard": 0.026},
        "premarket_calibration": {"enabled": True, "use_config_values": True, "trigger_estimate_weights": {"a1": 0.1, "a2": 0.05}, "bounds": {"A1_thr_sigma": {"min": 0.0018, "max": 0.0085}, "A2_grid_g": {"min": 0.12, "max": 0.62}, "k_atr": {"min": 1.3, "max": 2.5}, "A1_rvol_lo": {"min": 0.85, "max": 0.95}, "A1_rvol_hi": {"min": 1.6, "max": 2.0}, "A1_time_exit_min": {"min": 22, "max": 52}, "pause_theta_spread": {"min": 0.0001, "max": 0.0007}, "B1_rvol_min": {"min": 0.80, "max": 0.90}}, "forward_validation": {"days": 30, "top_k": 3, "score_alpha": 1.0, "score_beta": 1.0}, "baseline_shrinkage": {"enabled": True, "min_weight": 0.5, "max_weight": 0.9}, "cold_start_check": {"enabled": True, "duration_min": 1, "edge_coverage_threshold": 0.70, "trigger_rate_threshold": 0.50}}
    }
}

# Common sections for all configs
COMMON_SECTIONS = {
    "anti_starvation": {
        "enabled": True,
        "L1_min": 120,
        "L2_min": 180,
        "relax_sigma": [0.9, 0.85],
        "relax_g": [0.9, 0.85],
        "probe_ratio": 0.5,
        "probe_size_mult": 0.5,
        "revert_after_losses": 3,
        "cutoff": "14:00",
        "rvol_window_expand_pct": [0.05, 0.10],
        "rvol_window_shrink_pct": [0.05, 0.10]
    },
    "lkg": {
        "enabled": True,
        "adjustment_factors": {
            "k_atr": 1.10,
            "A1_thr_sigma": 1.05,
            "A2_grid_g": 1.05
        }
    },
    "l1_quality": {
        "enabled": True,
        "window_seconds": 60,
        "percentile_threshold": 10,
        "lookback_days": 30,
        "pause_duration_min": 2,
        "metrics": {
            "update_frequency": True,
            "latency": True,
            "negative_spread_ratio": True
        }
    },
    "kill_switch": {
        "enabled": True,
        "atr_mult_1m": 3.0,
        "luld_halt": True,
        "l1_disconnect_seconds": 60
    },
    "qqq_filler": {
        "enabled": True,
        "no_trade_threshold_min": 30,
        "cap_ratio": 0.5
    }
}

def create_complete_config(symbol):
    """Create complete config for a symbol"""
    config = {
        "symbol": symbol,
        "asof": "2025-10-08"
    }

    # Add base template
    config.update(BASE_TEMPLATE)

    # Add symbol-specific config
    if symbol in SYMBOL_CONFIGS:
        symbol_config = SYMBOL_CONFIGS[symbol]
        config["description"] = symbol_config["description"]
        config.update(symbol_config)

    # Add common sections
    config.update(COMMON_SECTIONS)
    config["lkg"]["file"] = f"{symbol.lower()}_v7_2_lkg.json"

    return config

def main():
    """Generate all config files"""
    configs_dir = Path("strategies/configs")

    for symbol in SYMBOL_CONFIGS.keys():
        config = create_complete_config(symbol)

        output_file = configs_dir / f"{symbol.lower()}_v7_2.json"
        with open(output_file, 'w') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        print(f"✅ Created complete config for {symbol}: {output_file}")

if __name__ == "__main__":
    main()
