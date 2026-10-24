"""Live-vs-replay calibration helpers for the option replay engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd


@dataclass
class CalibrationConfig:
    match_window_min: int = 3


def load_live_fills_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if df.empty:
        return df
    required = {"fill_ts", "side", "expiry", "strike", "qty", "fill_price"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"live fills csv missing columns: {sorted(missing)}")
    df = df.copy()
    df["fill_ts"] = pd.to_datetime(df["fill_ts"])
    df["side"] = df["side"].astype(str).str.upper()
    df["expiry"] = df["expiry"].astype(str)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int)
    df["fill_price"] = pd.to_numeric(df["fill_price"], errors="coerce")
    return df


def build_replay_fill_comparison_df(result: Dict[str, object]) -> pd.DataFrame:
    fills = result.get("fills")
    if not isinstance(fills, pd.DataFrame) or fills.empty:
        return pd.DataFrame(columns=["fill_ts", "side", "expiry", "strike", "qty", "fill_price", "order_id", "fill_reason"])
    out = fills.copy()
    out = out.rename(columns={"ts": "fill_ts"})
    keep = ["fill_ts", "side", "expiry", "strike", "qty", "fill_price", "order_id", "fill_reason"]
    for col in keep:
        if col not in out.columns:
            out[col] = pd.NA
    out["fill_ts"] = pd.to_datetime(out["fill_ts"])
    out["side"] = out["side"].astype(str).str.upper()
    out["expiry"] = out["expiry"].astype(str)
    out["strike"] = pd.to_numeric(out["strike"], errors="coerce")
    out["qty"] = pd.to_numeric(out["qty"], errors="coerce").fillna(0).astype(int)
    out["fill_price"] = pd.to_numeric(out["fill_price"], errors="coerce")
    return out[keep].reset_index(drop=True)


def match_live_to_replay_fills(
    replay_fills: pd.DataFrame,
    live_fills: pd.DataFrame,
    cfg: CalibrationConfig | None = None,
) -> pd.DataFrame:
    cfg = cfg or CalibrationConfig()
    if replay_fills.empty or live_fills.empty:
        return pd.DataFrame(
            columns=[
                "live_idx",
                "replay_idx",
                "matched",
                "time_diff_sec",
                "price_diff",
                "qty_diff",
                "live_fill_ts",
                "replay_fill_ts",
                "side",
                "expiry",
                "strike",
            ]
        )

    replay = replay_fills.reset_index(drop=True).copy()
    live = live_fills.reset_index(drop=True).copy()
    used_replay: set[int] = set()
    rows: List[Dict[str, object]] = []
    window_sec = int(cfg.match_window_min * 60)

    for live_idx, live_row in live.iterrows():
        candidates: List[tuple[int, float]] = []
        for replay_idx, replay_row in replay.iterrows():
            if replay_idx in used_replay:
                continue
            if str(live_row["side"]).upper() != str(replay_row["side"]).upper():
                continue
            if str(live_row["expiry"]) != str(replay_row["expiry"]):
                continue
            if abs(float(live_row["strike"]) - float(replay_row["strike"])) > 1e-9:
                continue
            time_diff = abs((pd.Timestamp(live_row["fill_ts"]) - pd.Timestamp(replay_row["fill_ts"])).total_seconds())
            if time_diff > window_sec:
                continue
            candidates.append((replay_idx, time_diff))

        if not candidates:
            rows.append(
                {
                    "live_idx": int(live_idx),
                    "replay_idx": pd.NA,
                    "matched": False,
                    "time_diff_sec": pd.NA,
                    "price_diff": pd.NA,
                    "qty_diff": pd.NA,
                    "live_fill_ts": live_row["fill_ts"],
                    "replay_fill_ts": pd.NaT,
                    "side": live_row["side"],
                    "expiry": live_row["expiry"],
                    "strike": live_row["strike"],
                }
            )
            continue

        replay_idx, time_diff = min(candidates, key=lambda item: item[1])
        used_replay.add(replay_idx)
        replay_row = replay.iloc[replay_idx]
        rows.append(
            {
                "live_idx": int(live_idx),
                "replay_idx": int(replay_idx),
                "matched": True,
                "time_diff_sec": float(time_diff),
                "price_diff": float(replay_row["fill_price"]) - float(live_row["fill_price"]),
                "qty_diff": int(replay_row["qty"]) - int(live_row["qty"]),
                "live_fill_ts": live_row["fill_ts"],
                "replay_fill_ts": replay_row["fill_ts"],
                "side": live_row["side"],
                "expiry": live_row["expiry"],
                "strike": live_row["strike"],
            }
        )
    return pd.DataFrame(rows)


def build_calibration_report(matches: pd.DataFrame) -> Dict[str, object]:
    if matches.empty:
        return {
            "live_rows": 0,
            "matched_rows": 0,
            "match_rate": 0.0,
            "avg_time_diff_sec": None,
            "avg_price_diff": None,
            "avg_abs_price_diff": None,
            "avg_qty_diff": None,
        }
    matched = matches[matches["matched"] == True]  # noqa: E712
    if matched.empty:
        return {
            "live_rows": int(len(matches)),
            "matched_rows": 0,
            "match_rate": 0.0,
            "avg_time_diff_sec": None,
            "avg_price_diff": None,
            "avg_abs_price_diff": None,
            "avg_qty_diff": None,
        }
    return {
        "live_rows": int(len(matches)),
        "matched_rows": int(len(matched)),
        "match_rate": float(len(matched) / max(1, len(matches))),
        "avg_time_diff_sec": float(matched["time_diff_sec"].mean()),
        "avg_price_diff": float(matched["price_diff"].mean()),
        "avg_abs_price_diff": float(matched["price_diff"].abs().mean()),
        "avg_qty_diff": float(matched["qty_diff"].mean()),
    }


__all__ = [
    "CalibrationConfig",
    "build_calibration_report",
    "build_replay_fill_comparison_df",
    "load_live_fills_csv",
    "match_live_to_replay_fills",
]
