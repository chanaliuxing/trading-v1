"""
tools/backfill_option_intraday_signals.py

Backfill option_intraday_signals for a historical range using existing models.

Usage:
  venv/bin/python tools/backfill_option_intraday_signals.py \
    --start 2025-12-01 --end 2026-01-01 \
    --model-version options_long_v1_20260106_024626 \
    --structure-model-version options_struct_v1_20260106_032153 \
    --top-n 1 \
    --replace
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys
from typing import Dict, List

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger
from services.options.ml_intraday_signal_service import (
    _align_features,
    _load_structure_catalog,
    _score_structure_candidates,
    _select_structure_for_symbol,
    StructureDef,
)

logger = get_logger(__name__)


def _load_models(model_version: str) -> tuple[object, object]:
    base_dir = "models/options_intraday"
    clf_dir = joblib.load(f"{base_dir}/{model_version}_clf_dir.pkl")
    clf_move = joblib.load(f"{base_dir}/{model_version}_clf_move.pkl")
    return clf_dir, clf_move


def _load_structure_ranker(model_version: str) -> dict | None:
    base_dir = "models/options_intraday"
    path = f"{base_dir}/{model_version}_structure_rank.pkl"
    if not Path(path).exists():
        return None
    return joblib.load(path)


def _fetch_features(start: str, end: str, universe: List[str]) -> pd.DataFrame:
    client = get_client()
    universe_list = ",".join(f"'{s}'" for s in universe)
    sql = f"""
    SELECT *
    FROM trading.features_1m
    WHERE symbol IN ({universe_list})
      AND ts >= toDateTime(%(start)s, 'America/New_York')
      AND ts <  toDateTime(%(end)s,   'America/New_York')
    ORDER BY ts, symbol
    """
    rows = client.query(sql, parameters={"start": start, "end": end})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _prepare_X(df: pd.DataFrame) -> tuple[pd.DataFrame, List[str]]:
    drop_cols = [c for c in ("symbol", "ts") if c in df.columns]
    X = df.drop(columns=drop_cols, errors="ignore")
    symbols = df["symbol"].tolist()
    return X, symbols


def _decide_actions(
    p_up: np.ndarray,
    p_down: np.ndarray,
    p_move: np.ndarray,
    theta_dir: float,
    theta_move: float,
    min_dir_diff: float,
    theta_move_credit: float,
    min_dir_diff_credit: float,
    max_dir_diff_neutral: float,
) -> List[str]:
    actions: List[str] = []
    for i in range(len(p_up)):
        dir_diff = p_up[i] - p_down[i]
        dir_max = max(p_up[i], p_down[i])
        if p_move[i] < theta_move_credit:
            if abs(dir_diff) <= max_dir_diff_neutral:
                actions.append("NEUTRAL")
            elif dir_diff >= min_dir_diff_credit:
                actions.append("BULLISH_CREDIT")
            elif -dir_diff >= min_dir_diff_credit:
                actions.append("BEARISH_CREDIT")
            else:
                actions.append("NO_TRADE")
            continue
        if p_move[i] < theta_move or dir_max < theta_dir:
            actions.append("NO_TRADE")
            continue
        if dir_diff >= min_dir_diff:
            actions.append("BUY_CALL")
        elif -dir_diff >= min_dir_diff:
            actions.append("BUY_PUT")
        else:
            actions.append("LONG_VOL_NEUTRAL")
    return actions


def _rank_top_n(df: pd.DataFrame, edge_scores: List[float], top_n: int) -> List[int]:
    ranks = [0] * len(df)
    df = df.copy()
    df["edge_score"] = edge_scores
    for ts, idxs in df.groupby("ts").groups.items():
        idxs = list(idxs)
        idxs_sorted = sorted(idxs, key=lambda i: edge_scores[i], reverse=True)
        for rank, idx in enumerate(idxs_sorted[:top_n], start=1):
            ranks[idx] = rank
    return ranks


def _fallback_structure_for_action(action: str, structures: List[StructureDef]) -> StructureDef | None:
    action_map = {
        "BUY_CALL": {"SINGLE_CALL", "VERTICAL_DEBIT_CALL", "CALL_CALENDAR", "CALL_DIAGONAL"},
        "BUY_PUT": {"SINGLE_PUT", "VERTICAL_DEBIT_PUT", "PUT_CALENDAR", "PUT_DIAGONAL"},
        "BULLISH_CREDIT": {"VERTICAL_CREDIT_PUT"},
        "BEARISH_CREDIT": {"VERTICAL_CREDIT_CALL"},
        "NEUTRAL": {"IRON_CONDOR", "IRON_BUTTERFLY", "CALL_BUTTERFLY", "PUT_BUTTERFLY"},
        "LONG_VOL_NEUTRAL": {"LONG_STRADDLE", "LONG_STRANGLE"},
    }
    allowed = action_map.get(action)
    if not allowed:
        return None
    for s in structures:
        if s.structure_type in allowed:
            return s
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--structure-model-version", required=True)
    parser.add_argument("--top-n", type=int, default=1)
    parser.add_argument("--theta-dir", type=float, default=0.6)
    parser.add_argument("--theta-move", type=float, default=0.65)
    parser.add_argument("--min-dir-diff", type=float, default=0.1)
    parser.add_argument("--theta-move-credit", type=float, default=0.4)
    parser.add_argument("--min-dir-diff-credit", type=float, default=0.05)
    parser.add_argument("--max-dir-diff-neutral", type=float, default=0.05)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    universe = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"]
    df = _fetch_features(args.start, args.end, universe)
    if df.empty:
        logger.warning("No features fetched", start=args.start, end=args.end)
        return

    clf_dir, clf_move = _load_models(args.model_version)
    ranker = _load_structure_ranker(args.structure_model_version)
    structures = _load_structure_catalog([])

    X, symbols = _prepare_X(df)
    X = _align_features(X, clf_dir)

    proba_dir = clf_dir.predict_proba(X)
    classes_dir = clf_dir.classes_
    class_to_idx = {int(c): i for i, c in enumerate(classes_dir)}
    p_up = proba_dir[:, class_to_idx.get(1, 0)]
    p_down = proba_dir[:, class_to_idx.get(-1, 0)]
    p_flat = proba_dir[:, class_to_idx.get(0, 0)]

    proba_move = clf_move.predict_proba(X)
    classes_move = clf_move.classes_
    idx_move1 = int(np.where(classes_move == 1)[0][0]) if 1 in classes_move else 1
    p_move = proba_move[:, idx_move1]

    actions = _decide_actions(
        p_up,
        p_down,
        p_move,
        args.theta_dir,
        args.theta_move,
        args.min_dir_diff,
        args.theta_move_credit,
        args.min_dir_diff_credit,
        args.max_dir_diff_neutral,
    )
    edge_scores: List[float] = []
    structure_by_symbol: Dict[str, StructureDef] = {}
    legs_specs: List[str] = []

    for i, sym in enumerate(symbols):
        edge = float(p_move[i] * max(p_up[i], p_down[i]))
        structure, legs_spec = _select_structure_for_symbol(actions[i], structures, {})
        if actions[i] != "NO_TRADE" and legs_spec == "{}":
            fallback = _fallback_structure_for_action(actions[i], structures)
            if fallback:
                structure = fallback
                legs_spec = _select_structure_for_symbol(actions[i], [fallback], {})[1]
        if ranker and actions[i] != "NO_TRADE":
            cand = []
            for s in structures:
                chosen, _ = _select_structure_for_symbol(actions[i], [s], {})
                if chosen:
                    cand.append(s)
            best, score = _score_structure_candidates(X.iloc[i], cand, ranker)
            if best:
                structure = best
                edge = score
                legs_spec = _select_structure_for_symbol(actions[i], [best], {})[1]

        edge_scores.append(edge if actions[i] != "NO_TRADE" else 0.0)
        if structure:
            structure_by_symbol[sym] = structure
        legs_specs.append(legs_spec)

    ranks = _rank_top_n(df, edge_scores, args.top_n)

    if args.replace:
        client = get_client()
        client.command(
            """
            ALTER TABLE trading.option_intraday_signals
            DELETE WHERE ts >= toDateTime(%(start)s, 'America/New_York')
              AND ts <  toDateTime(%(end)s,   'America/New_York')
            """,
            parameters={"start": args.start, "end": args.end},
        )

    rows = []
    for i in range(len(df)):
        sym = symbols[i]
        rows.append(
            (
                df["ts"].iloc[i],
                sym,
                actions[i] if ranks[i] > 0 else "NO_TRADE",
                30,
                float(p_up[i]),
                float(p_down[i]),
                float(p_flat[i]),
                float(p_move[i]),
                args.model_version,
                args.theta_dir,
                args.theta_move,
                "",
                json.dumps({"structure_model_version": args.structure_model_version}),
                structure_by_symbol.get(sym).structure_id if ranks[i] > 0 and sym in structure_by_symbol else "",
                structure_by_symbol.get(sym).structure_type if ranks[i] > 0 and sym in structure_by_symbol else "",
                legs_specs[i] if ranks[i] > 0 else "{}",
                float(edge_scores[i]),
                int(ranks[i]),
                0.0,
                0.0,
                "{}",
            )
        )

    client = get_client()
    client.insert(
        "trading.option_intraday_signals",
        rows,
        column_names=[
            "ts",
            "symbol",
            "action",
            "horizon_min",
            "p_up",
            "p_down",
            "p_flat",
            "p_move",
            "model_version",
            "theta_dir",
            "theta_move",
            "reason_code",
            "tags",
            "structure_id",
            "structure_type",
            "legs_spec_json",
            "edge_score",
            "rank",
            "max_loss_est",
            "net_debit_est",
            "liquidity_flags",
        ],
    )
    logger.info("Backfilled option_intraday_signals", rows=len(rows))


if __name__ == "__main__":
    main()
