# feature_templates.py
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations
from typing import Dict, Any, List

def momentum_template(ret_col: str = "ret_1", windows: List[int] = [10, 30, 60]) -> Dict[str, Any]:
    steps = []
    for w in windows:
        steps.append({"name": f"momo_{w}", "op": "rolling_sum", "col": ret_col, "window_bars": w, "per_symbol": True})
    steps.append({"name": "momo_vol_20", "op": "rolling_std", "col": ret_col, "window_bars": 20, "per_symbol": True})
    steps.append({"name": "momo_10_vol", "op": "ratio", "num": "momo_10", "den": "momo_vol_20", "eps": 1e-6})
    return {"description": "momentum multi window", "steps": steps}

def microstructure_template() -> Dict[str, Any]:
    return {
        "description": "liquidity & impact",
        "steps": [
            {"name": "eff_spread", "op": "effective_spread"},
            {"name": "amihud", "op": "amihud_illiquidity", "eps": 1e-6},
            {"name": "impact_hl", "op": "price_impact_hl"},
        ],
    }

TEMPLATES = {
    "template.momentum": momentum_template,
    "template.microstructure": microstructure_template,
}

def expand_template(template_name: str, **kwargs) -> Dict[str, Any]:
    if template_name not in TEMPLATES:
        raise KeyError(f"Unknown template: {template_name}")
    return TEMPLATES[template_name](**kwargs)
