"""
tools/seed_option_structure_catalog.py

Seed trading.option_structure_catalog with a minimal multi-leg set:
- SINGLE_CALL
- SINGLE_PUT
- VERTICAL_DEBIT_CALL
- VERTICAL_DEBIT_PUT

Usage:
  venv/bin/python tools/seed_option_structure_catalog.py --replace
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _structures() -> list[dict]:
    return [
        # --- Long premium (directional) ---
        {
            "structure_id": "SINGLE_CALL_D0_35",
            "structure_type": "SINGLE_CALL",
            "legs_template": [
                {"right": "C", "side": "BUY", "qty": 1, "delta_target": 0.35, "anchor": True, "anchor_id": "call"}
            ],
            "param_defaults": {
                "dte_candidates": [5, 7, 10],
                "delta_target": 0.35,
                "max_spread_bps": 30,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "SINGLE_PUT_D0_35",
            "structure_type": "SINGLE_PUT",
            "legs_template": [
                {"right": "P", "side": "BUY", "qty": 1, "delta_target": 0.35, "anchor": True, "anchor_id": "put"}
            ],
            "param_defaults": {
                "dte_candidates": [5, 7, 10],
                "delta_target": 0.35,
                "max_spread_bps": 30,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "VERTICAL_DEBIT_CALL_W5",
            "structure_type": "VERTICAL_DEBIT_CALL",
            "legs_template": [
                {"right": "C", "side": "BUY", "qty": 1, "delta_target": 0.35, "anchor": True, "anchor_id": "call"},
                {"right": "C", "side": "SELL", "qty": 1, "strike_offset": 5.0, "anchor_ref": "call"},
            ],
            "param_defaults": {
                "dte_candidates": [5, 7, 10],
                "delta_target": 0.35,
                "width": 5.0,
                "max_spread_bps": 40,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "VERTICAL_DEBIT_PUT_W5",
            "structure_type": "VERTICAL_DEBIT_PUT",
            "legs_template": [
                {"right": "P", "side": "BUY", "qty": 1, "delta_target": 0.35, "anchor": True, "anchor_id": "put"},
                {"right": "P", "side": "SELL", "qty": 1, "strike_offset": -5.0, "anchor_ref": "put"},
            ],
            "param_defaults": {
                "dte_candidates": [5, 7, 10],
                "delta_target": 0.35,
                "width": 5.0,
                "max_spread_bps": 40,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        # --- Credit spreads (defined-risk short premium) ---
        {
            "structure_id": "VERTICAL_CREDIT_PUT_W5",
            "structure_type": "VERTICAL_CREDIT_PUT",
            "legs_template": [
                {"right": "P", "side": "SELL", "qty": 1, "delta_target": 0.30, "anchor": True, "anchor_id": "put"},
                {"right": "P", "side": "BUY", "qty": 1, "strike_offset": -5.0, "anchor_ref": "put"},
            ],
            "param_defaults": {
                "dte_candidates": [5, 7, 10],
                "delta_target": 0.30,
                "width": 5.0,
                "max_spread_bps": 40,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "VERTICAL_CREDIT_CALL_W5",
            "structure_type": "VERTICAL_CREDIT_CALL",
            "legs_template": [
                {"right": "C", "side": "SELL", "qty": 1, "delta_target": 0.30, "anchor": True, "anchor_id": "call"},
                {"right": "C", "side": "BUY", "qty": 1, "strike_offset": 5.0, "anchor_ref": "call"},
            ],
            "param_defaults": {
                "dte_candidates": [5, 7, 10],
                "delta_target": 0.30,
                "width": 5.0,
                "max_spread_bps": 40,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        # --- Neutral defined-risk ---
        {
            "structure_id": "IRON_CONDOR_W5",
            "structure_type": "IRON_CONDOR",
            "legs_template": [
                {"right": "P", "side": "SELL", "qty": 1, "delta_target": 0.20, "anchor": True, "anchor_id": "put"},
                {"right": "P", "side": "BUY", "qty": 1, "strike_offset": -5.0, "anchor_ref": "put"},
                {"right": "C", "side": "SELL", "qty": 1, "delta_target": 0.20, "anchor": True, "anchor_id": "call"},
                {"right": "C", "side": "BUY", "qty": 1, "strike_offset": 5.0, "anchor_ref": "call"},
            ],
            "param_defaults": {
                "dte_candidates": [7, 10, 14],
                "delta_target": 0.20,
                "width": 5.0,
                "max_spread_bps": 50,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "IRON_BUTTERFLY_W5",
            "structure_type": "IRON_BUTTERFLY",
            "legs_template": [
                {"right": "C", "side": "SELL", "qty": 1, "delta_target": 0.50, "anchor": True, "anchor_id": "center"},
                {"right": "P", "side": "SELL", "qty": 1, "strike_offset": 0.0, "anchor_ref": "center"},
                {"right": "C", "side": "BUY", "qty": 1, "strike_offset": 5.0, "anchor_ref": "center"},
                {"right": "P", "side": "BUY", "qty": 1, "strike_offset": -5.0, "anchor_ref": "center"},
            ],
            "param_defaults": {
                "dte_candidates": [7, 10, 14],
                "delta_target": 0.50,
                "width": 5.0,
                "max_spread_bps": 60,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        # --- Butterflies (defined-risk) ---
        {
            "structure_id": "CALL_BUTTERFLY_W5",
            "structure_type": "CALL_BUTTERFLY",
            "legs_template": [
                {"right": "C", "side": "SELL", "qty": 2, "delta_target": 0.50, "anchor": True, "anchor_id": "center"},
                {"right": "C", "side": "BUY", "qty": 1, "strike_offset": -5.0, "anchor_ref": "center"},
                {"right": "C", "side": "BUY", "qty": 1, "strike_offset": 5.0, "anchor_ref": "center"},
            ],
            "param_defaults": {
                "dte_candidates": [7, 10, 14],
                "delta_target": 0.50,
                "width": 5.0,
                "max_spread_bps": 60,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "PUT_BUTTERFLY_W5",
            "structure_type": "PUT_BUTTERFLY",
            "legs_template": [
                {"right": "P", "side": "SELL", "qty": 2, "delta_target": 0.50, "anchor": True, "anchor_id": "center"},
                {"right": "P", "side": "BUY", "qty": 1, "strike_offset": -5.0, "anchor_ref": "center"},
                {"right": "P", "side": "BUY", "qty": 1, "strike_offset": 5.0, "anchor_ref": "center"},
            ],
            "param_defaults": {
                "dte_candidates": [7, 10, 14],
                "delta_target": 0.50,
                "width": 5.0,
                "max_spread_bps": 60,
                "min_oi": 500,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        # --- Calendars / diagonals (defined-risk) ---
        {
            "structure_id": "CALL_CALENDAR",
            "structure_type": "CALL_CALENDAR",
            "legs_template": [
                {"right": "C", "side": "BUY", "qty": 1, "delta_target": 0.35, "anchor": True, "anchor_id": "call_long"},
                {"right": "C", "side": "SELL", "qty": 1, "strike_offset": 0.0, "dte_target": 7, "anchor_ref": "call_long"},
            ],
            "param_defaults": {
                "dte_candidates": [21, 30, 45],
                "delta_target": 0.35,
                "max_spread_bps": 50,
                "min_oi": 300,
                "min_volume": 50,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "PUT_CALENDAR",
            "structure_type": "PUT_CALENDAR",
            "legs_template": [
                {"right": "P", "side": "BUY", "qty": 1, "delta_target": 0.35, "anchor": True, "anchor_id": "put_long"},
                {"right": "P", "side": "SELL", "qty": 1, "strike_offset": 0.0, "dte_target": 7, "anchor_ref": "put_long"},
            ],
            "param_defaults": {
                "dte_candidates": [21, 30, 45],
                "delta_target": 0.35,
                "max_spread_bps": 50,
                "min_oi": 300,
                "min_volume": 50,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "CALL_DIAGONAL_W5",
            "structure_type": "CALL_DIAGONAL",
            "legs_template": [
                {"right": "C", "side": "BUY", "qty": 1, "delta_target": 0.35, "anchor": True, "anchor_id": "call_long"},
                {"right": "C", "side": "SELL", "qty": 1, "strike_offset": 5.0, "dte_target": 7, "anchor_ref": "call_long"},
            ],
            "param_defaults": {
                "dte_candidates": [21, 30, 45],
                "delta_target": 0.35,
                "width": 5.0,
                "max_spread_bps": 60,
                "min_oi": 300,
                "min_volume": 50,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "PUT_DIAGONAL_W5",
            "structure_type": "PUT_DIAGONAL",
            "legs_template": [
                {"right": "P", "side": "BUY", "qty": 1, "delta_target": 0.35, "anchor": True, "anchor_id": "put_long"},
                {"right": "P", "side": "SELL", "qty": 1, "strike_offset": -5.0, "dte_target": 7, "anchor_ref": "put_long"},
            ],
            "param_defaults": {
                "dte_candidates": [21, 30, 45],
                "delta_target": 0.35,
                "width": 5.0,
                "max_spread_bps": 60,
                "min_oi": 300,
                "min_volume": 50,
            },
            "enabled": 1,
            "version": "v1",
        },
        # --- Long vol (defined-risk) ---
        {
            "structure_id": "LONG_STRADDLE",
            "structure_type": "LONG_STRADDLE",
            "legs_template": [
                {"right": "C", "side": "BUY", "qty": 1, "delta_target": 0.50, "anchor": True, "anchor_id": "center"},
                {"right": "P", "side": "BUY", "qty": 1, "strike_offset": 0.0, "anchor_ref": "center"},
            ],
            "param_defaults": {
                "dte_candidates": [5, 7, 10],
                "delta_target": 0.50,
                "max_spread_bps": 60,
                "min_oi": 300,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
        {
            "structure_id": "LONG_STRANGLE_W5",
            "structure_type": "LONG_STRANGLE",
            "legs_template": [
                {"right": "C", "side": "BUY", "qty": 1, "delta_target": 0.30, "anchor": True, "anchor_id": "center"},
                {"right": "P", "side": "BUY", "qty": 1, "strike_offset": -5.0, "anchor_ref": "center"},
            ],
            "param_defaults": {
                "dte_candidates": [5, 7, 10],
                "delta_target": 0.30,
                "width": 5.0,
                "max_spread_bps": 70,
                "min_oi": 300,
                "min_volume": 100,
            },
            "enabled": 1,
            "version": "v1",
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replace", action="store_true", help="Delete existing rows before insert")
    args = parser.parse_args()

    client = get_client()
    if args.replace:
        client.command("ALTER TABLE trading.option_structure_catalog DELETE WHERE 1")

    rows = []
    for s in _structures():
        rows.append(
            (
                s["structure_id"],
                s["structure_type"],
                json.dumps(s["legs_template"], ensure_ascii=False),
                json.dumps(s["param_defaults"], ensure_ascii=False),
                int(s["enabled"]),
                s["version"],
                datetime.now(),
            )
        )

    client.insert(
        "trading.option_structure_catalog",
        rows,
        column_names=[
            "structure_id",
            "structure_type",
            "legs_template_json",
            "param_defaults_json",
            "enabled",
            "version",
            "created_at",
        ],
    )
    logger.info("Seeded option_structure_catalog", rows=len(rows))


if __name__ == "__main__":
    main()
