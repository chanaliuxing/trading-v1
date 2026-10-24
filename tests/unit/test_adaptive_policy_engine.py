#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.enhanced_model_signal_strategy import AdaptivePolicyEngine


def test_adaptive_vix_and_exec_overrides():
    engine = AdaptivePolicyEngine()
    sym_cfg = {
        "adaptive": {
            "enabled": True,
            "rules": [
                {"when": {"vix_gt": 20}, "overrides": {"conf_floor": 0.7, "sizer_scale": 0.75}},
                {"when": {"spy_abs_ret_1m_gt": 0.8}, "overrides": {"exec_mode": "TWAP", "exec_horizon_s": 120}},
                {"when": {"gap_dn_pct_ge": 3.0}, "overrides": {"risk_stop_loss_pct": 0.015}},
            ]
        }
    }
    features = {"vix": 25.0, "spy_ret_1m": 0.012, "gap_pct": -0.035}
    o = engine.evaluate("TSLA", sym_cfg, features)
    # Combined overrides present
    assert o.get("conf_floor") == 0.7
    assert o.get("sizer_scale") == 0.75
    assert o.get("exec_mode") == "TWAP"
    assert o.get("exec_horizon_s") == 120
    # Risk override applied
    assert abs(o.get("risk_stop_loss_pct") - 0.015) < 1e-9

