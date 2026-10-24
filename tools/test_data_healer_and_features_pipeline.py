#!/usr/bin/env python3
"""
Lightweight sanity checks for the data self‑healing + enhanced feature pipeline.

This script is designed to be safe to run on any environment:
 - It does NOT require a live ClickHouse instance.
 - It does NOT talk to IBKR or run real backfills.

Instead it:
 1) Stubs out ClickHouseDB inside DataHealer to exercise the coverage logic and
    verify that a "healthy" symbol set does not trigger backfill.
 2) Runs FeatureEngineerV2 on a small synthetic DataFrame with deliberate NaNs
    to ensure that compute_all_features() produces non‑NaN values for the
    enhanced columns we rely on in production.

Usage:
    venv/bin/python tools/test_data_healer_and_features_pipeline.py
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List
import sys

import numpy as np
import pandas as pd

# Ensure project root (repo root) is on sys.path so that "services.*" imports work
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.data.data_healer import DataHealer
from libs.features.feature_engineer_v2 import FeatureEngineerV2


class _FakeClickHouse:
    """
    Minimal stub for ClickHouseDB used by DataHealer in tests.

    It implements only the .query() method and returns a fixed count per symbol
    so we can exercise _check_coverage / ensure_data_ready without touching a
    real database.
    """

    def __init__(self, counts: Dict[str, int]) -> None:
        # mimic ClickHouseDB.config shape used by DataHealer
        self.config: Dict[str, Any] = {"host": "localhost", "port": 8123}
        self._counts = counts

    def query(self, sql: str, parameters: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        # Return one row per symbol with the preconfigured count.
        out: List[Dict[str, Any]] = []
        for sym, cnt in self._counts.items():
            out.append({"symbol": sym, "cnt": int(cnt)})
        return out


def _test_data_healer_logic() -> None:
    runs_dir = Path("runs/ai_trader")
    runs_dir.mkdir(parents=True, exist_ok=True)

    healer = DataHealer(str(runs_dir))
    # Inject fake ClickHouse client so no real DB calls are made.
    healer.ch = _FakeClickHouse({"TSLA": 500, "NVDA": 400})

    ok = healer.ensure_data_ready(["TSLA", "NVDA"], lookback_days=2)
    print(f"[DataHealer] ensure_data_ready TSLA,NVDA → {ok}")
    if not ok:
        raise SystemExit("DataHealer test failed: expected ok=True with healthy fake coverage.")


def _test_feature_engineer_v2() -> None:
    # Build a tiny OHLCV sample with intentional NaNs in open/high/low/vwap
    idx = pd.date_range(
        start=_dt.datetime(2025, 11, 20, 9, 30),
        periods=120,
        freq="1min",
        tz="America/New_York",
    )
    close = np.linspace(250.0, 260.0, len(idx))
    volume = np.full(len(idx), 1_000.0)

    df = pd.DataFrame(
        {
            "symbol": ["TSLA"] * len(idx),
            "open": close.copy(),
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "vwap": close + 0.1,
            "volume": volume,
            "atr_14": np.full(len(idx), 1.0),
        },
        index=idx,
    )
    # Inject some NaNs to verify that compute_all_features() can handle them.
    df.loc[idx[5:10], "open"] = np.nan
    df.loc[idx[10:15], "high"] = np.nan
    df.loc[idx[15:20], "low"] = np.nan
    df.loc[idx[20:25], "vwap"] = np.nan

    eng = FeatureEngineerV2()
    df_enh = eng.compute_all_features(df, spy_df=None)

    core_enhanced = [
        "vol_5m",
        "vol_15m",
        "vol_1h",
        "realized_vol_5m",
        "realized_vol_15m",
        "vwap_dist",
        "vwap_dist_5m",
        "trade_imb_5m",
        "volume_surge",
        "price_momentum_5m",
        "reversion_1m",
        "volatility_ratio",
        "hour",
        "minute",
        "minutes_from_open",
        "minutes_to_close",
    ]

    missing_cols = [c for c in core_enhanced if c not in df_enh.columns]
    if missing_cols:
        raise SystemExit(f"FeatureEngineerV2 test failed: missing enhanced columns: {missing_cols}")

    # Drop the first 60 warm‑up rows where rolling windows are expected to be NaN.
    mid_window = df_enh.iloc[60:]
    nan_summary = mid_window[core_enhanced].isna().sum()
    print("[FeatureEngineerV2] NaN counts (post‑warmup) for core enhanced features:")
    print(nan_summary.to_dict())

    # Require that at least one non‑NaN value exists for each enhanced feature
    # after the warm‑up window. This ensures we are not accidentally producing
    # all‑NULL columns due to NaN handling regressions.
    all_null_cols = [c for c, cnt in nan_summary.items() if cnt >= len(mid_window)]
    if all_null_cols:
        raise SystemExit(f"FeatureEngineerV2 test failed: columns all-NaN after warmup: {all_null_cols}")


if __name__ == "__main__":
    print("=== Running DataHealer + FeatureEngineerV2 sanity checks ===")
    _test_data_healer_logic()
    _test_feature_engineer_v2()
    print("✅ All sanity checks passed.")
