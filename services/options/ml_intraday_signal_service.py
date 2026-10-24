"""
services/options/ml_intraday_signal_service.py

Online inference service for intraday long-only option ML:
- Loads active model version & thresholds from trading.option_ml_controls / option_ml_models.
- Every minute:
    * Reads latest 1m features for configured universe from trading.features_1m.
    * Computes p_up, p_down, p_flat, p_move.
    * Applies thresholds to decide action ∈ {BUY_CALL, BUY_PUT, NO_TRADE}.
    * Writes signals into trading.option_intraday_signals.

NOTE:
- This is a skeleton. Glue code for model loading, scheduling, and error
  handling should be integrated with your existing service runtime.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
import json
from typing import Dict, List, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

import joblib
import numpy as np
import pandas as pd

from libs.db.ch_pool import get_client
from libs.logger import get_logger
import os

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")

# Dedicated loggers setup
def _get_loggers():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S')

    # Trader Logger (Business Logic & Decisions)
    t_log = logging.getLogger("option_ml_trader")
    if not t_log.handlers:
        t_log.setLevel(logging.INFO)
        t_log.propagate = False
        fh_t = logging.FileHandler(os.path.join(log_dir, "option_ml_trader.log"), encoding='utf-8')
        fh_t.setFormatter(formatter)
        t_log.addHandler(fh_t)

    # System Logger (Pipeline, Data, Debugging)
    s_log = logging.getLogger("option_ml_system")
    if not s_log.handlers:
        s_log.setLevel(logging.INFO)
        s_log.propagate = False
        fh_s = logging.FileHandler(os.path.join(log_dir, "option_ml_system.log"), encoding='utf-8')
        fh_s.setFormatter(formatter)
        s_log.addHandler(fh_s)

    return t_log, s_log

trader_logger, system_logger = _get_loggers()


def _is_rth() -> bool:
    try:
        now_et = datetime.now(ET)
        rth_start = os.getenv("RTH_START", "09:30")
        rth_end = os.getenv("RTH_END", "16:00")
        start_h, start_m = [int(x) for x in rth_start.split(":")]
        end_h, end_m = [int(x) for x in rth_end.split(":")]
        start = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return start <= now_et <= end and now_et.weekday() < 5
    except Exception:
        return True


@dataclass
class ControlState:
    enabled: bool
    strategy_name: str
    active_model_version: str
    theta_dir: float
    theta_move: float
    min_dir_diff: float
    theta_move_credit: float
    min_dir_diff_credit: float
    max_dir_diff_neutral: float
    universe: List[str]
    horizon_min: int
    allowed_structures: List[str]
    structure_param_caps: Dict[str, dict]
    top_n: int
    structure_model_version: str
    extra_config: Dict[str, object]
    allow_stale_features: bool
    event_blacklist_dates: List[str]
    universe_override: List[str]


@dataclass
class StructureDef:
    structure_id: str
    structure_type: str
    legs_template: list
    param_defaults: dict


def _load_controls(strategy_name: str = "intraday_options_long") -> ControlState | None:
    client = get_client()
    sql = """
    SELECT
      strategy_name,
      updated_at,
      enabled,
      active_model_version,
      theta_dir,
      theta_move,
      max_trade_risk_pct,
      max_daily_dd_pct,
      max_positions_per_symbol,
      max_total_positions,
      extra_config,
      allowed_structures,
      structure_param_caps_json,
      top_n
    FROM trading.option_ml_controls
    WHERE strategy_name = %(strategy_name)s
    ORDER BY updated_at DESC
    LIMIT 1
    """
    rows = client.query(sql, parameters={"strategy_name": strategy_name})
    if not rows:
        logger.warning("No controls row found for strategy", strategy=strategy_name)
        return None

    row = rows[0]
    # Universe & horizon can be derived from option_ml_models
    model_rows = client.query(
        """
        SELECT universe, horizon_min
        FROM trading.option_ml_models
        WHERE model_version = %(model_version)s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        parameters={"model_version": row["active_model_version"]},
    )
    if not model_rows:
        logger.warning("No model metadata found for version", version=row["active_model_version"])
        return None

    m = model_rows[0]
    universe = m.get("universe") or []
    horizon_min = int(m.get("horizon_min") or 30)
    extra_config = _parse_extra_config(row.get("extra_config", ""))
    structure_model_version = str(extra_config.get("structure_model_version") or row["active_model_version"])
    allow_stale_features = bool(extra_config.get("allow_stale_features", False))
    event_blacklist_dates = extra_config.get("event_blacklist_dates", [])
    if not isinstance(event_blacklist_dates, list):
        event_blacklist_dates = []
    min_dir_diff = float(extra_config.get("min_dir_diff", 0.1))
    theta_move_credit = float(extra_config.get("theta_move_credit", 0.4))
    min_dir_diff_credit = float(extra_config.get("min_dir_diff_credit", 0.05))
    max_dir_diff_neutral = float(extra_config.get("max_dir_diff_neutral", 0.05))
    allowed_structures = _parse_allowed_structures(row.get("allowed_structures", ""))
    universe_override = extra_config.get("universe_override", [])
    if not isinstance(universe_override, list):
        universe_override = []
    if universe_override:
        universe = [str(s).upper() for s in universe_override if str(s).strip()]
    structure_param_caps = _parse_structure_caps(row.get("structure_param_caps_json", ""))
    top_n = int(row.get("top_n") or 1)

    return ControlState(
        enabled=bool(row["enabled"]),
        strategy_name=row["strategy_name"],
        active_model_version=row["active_model_version"],
        theta_dir=float(row["theta_dir"]),
        theta_move=float(row["theta_move"]),
        min_dir_diff=min_dir_diff,
        theta_move_credit=theta_move_credit,
        min_dir_diff_credit=min_dir_diff_credit,
        max_dir_diff_neutral=max_dir_diff_neutral,
        universe=list(universe),
        horizon_min=horizon_min,
        allowed_structures=allowed_structures,
        structure_param_caps=structure_param_caps,
        top_n=top_n,
        structure_model_version=structure_model_version,
        extra_config=extra_config,
        allow_stale_features=allow_stale_features,
        event_blacklist_dates=[str(d) for d in event_blacklist_dates],
        universe_override=list(universe_override),
    )


def _parse_allowed_structures(raw: str) -> List[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return [str(x) for x in data if str(x).strip()]
        except json.JSONDecodeError:
            return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _parse_structure_caps(raw: str) -> Dict[str, dict]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _parse_extra_config(raw: str) -> Dict[str, object]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _load_structure_catalog(allowed_structures: List[str]) -> List[StructureDef]:
    client = get_client()
    sql = """
    SELECT
      structure_id,
      structure_type,
      legs_template_json,
      param_defaults_json
    FROM trading.option_structure_catalog
    WHERE enabled = 1
    ORDER BY structure_id
    """
    rows = client.query(sql)
    by_id: Dict[str, StructureDef] = {}
    for row in rows:
        by_id[row["structure_id"]] = StructureDef(
            structure_id=row["structure_id"],
            structure_type=row["structure_type"],
            legs_template=json.loads(row["legs_template_json"] or "[]"),
            param_defaults=json.loads(row["param_defaults_json"] or "{}"),
        )

    if allowed_structures:
        return [by_id[sid] for sid in allowed_structures if sid in by_id]
    return list(by_id.values())


def _load_models(model_version: str) -> Tuple[object, object]:
    """
    Placeholder for loading clf_dir and clf_move from disk:
    - You can use joblib.load from a shared models directory, or
    - Implement your own model store.
    """
    # Example path convention; adjust to your environment:
    base_dir = os.getenv("OPTION_ML_MODEL_DIR", "models/options_intraday")
    path_dir = f"{base_dir}/{model_version}_clf_dir.pkl"
    path_move = f"{base_dir}/{model_version}_clf_move.pkl"

    clf_dir = joblib.load(path_dir)
    clf_move = joblib.load(path_move)
    return clf_dir, clf_move


def _load_structure_ranker(model_version: str) -> dict | None:
    base_dir = os.getenv("OPTION_ML_MODEL_DIR", "models/options_intraday")
    path = f"{base_dir}/{model_version}_structure_rank.pkl"
    if not os.path.exists(path):
        return None
    return joblib.load(path)


def _fetch_latest_features(universe: List[str], allow_stale: bool = False) -> pd.DataFrame:
    client = get_client()
    universe_list = ",".join(f"'{s}'" for s in universe)
    if allow_stale:
        sql = f"""
        SELECT *
        FROM trading.features_1m
        PREWHERE symbol IN ({universe_list})
        WHERE ts >= (SELECT max(ts) FROM trading.features_1m) - INTERVAL 7 DAY
        ORDER BY ts DESC
        LIMIT 1 BY symbol
        """
    else:
        sql = f"""
        SELECT *
        FROM trading.features_1m
        PREWHERE symbol IN ({universe_list})
        WHERE ts >= now('America/New_York') - INTERVAL 5 MINUTE
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) = 1
        """
    rows = client.query(sql)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _prepare_X(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    drop_cols = [c for c in ("symbol", "ts") if c in df.columns]
    X = df.drop(columns=drop_cols, errors="ignore")
    symbols = df["symbol"].tolist()
    return X, symbols


def _model_feature_names(model: object) -> List[str]:
    if hasattr(model, "feature_names_in_"):
        return list(getattr(model, "feature_names_in_"))
    estimator = getattr(model, "estimator", None)
    if estimator is not None and hasattr(estimator, "feature_names_in_"):
        return list(getattr(estimator, "feature_names_in_"))
    return []


def _align_features(X: pd.DataFrame, model: object) -> pd.DataFrame:
    feature_names = _model_feature_names(model)
    if not feature_names:
        return X
    for col in feature_names:
        if col not in X.columns:
            X[col] = 0.0
    X = X[feature_names]
    return X


def _write_signals(
    ts: pd.Series,
    symbols: List[str],
    actions: List[str],
    p_up: np.ndarray,
    p_down: np.ndarray,
    p_flat: np.ndarray,
    p_move: np.ndarray,
    structure_defs: Dict[str, StructureDef],
    legs_specs: List[str],
    edge_scores: List[float],
    ranks: List[int],
    reason_codes: List[str],
    tags_list: List[str],
    ctrl: ControlState,
):
    client = get_client()
    data = []
    for i, sym in enumerate(symbols):
        data.append(
            (
                ts.iloc[i],
                sym,
                actions[i],
                ctrl.horizon_min,
                float(p_up[i]),
                float(p_down[i]),
                float(p_flat[i]),
                float(p_move[i]),
                ctrl.active_model_version,
                ctrl.theta_dir,
                ctrl.theta_move,
                reason_codes[i],
                tags_list[i],
                structure_defs.get(sym).structure_id if structure_defs.get(sym) else "",
                structure_defs.get(sym).structure_type if structure_defs.get(sym) else "",
                legs_specs[i],
                float(edge_scores[i]),
                int(ranks[i]),
                0.0,
                0.0,
                "{}",
            )
        )

    if not data:
        return

    client.insert(
        "trading.option_intraday_signals",
        data,
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


def _select_structure_for_symbol(
    action: str,
    structures: List[StructureDef],
    caps: Dict[str, dict],
) -> Tuple[StructureDef | None, str]:
    action_map = {
        "BUY_CALL": {"SINGLE_CALL", "VERTICAL_DEBIT_CALL", "CALL_CALENDAR", "CALL_DIAGONAL"},
        "BUY_PUT": {"SINGLE_PUT", "VERTICAL_DEBIT_PUT", "PUT_CALENDAR", "PUT_DIAGONAL"},
        "BULLISH_CREDIT": {"VERTICAL_CREDIT_PUT"},
        "BEARISH_CREDIT": {"VERTICAL_CREDIT_CALL"},
        "NEUTRAL": {"IRON_CONDOR", "IRON_BUTTERFLY", "CALL_BUTTERFLY", "PUT_BUTTERFLY"},
        "LONG_VOL_NEUTRAL": {"LONG_STRADDLE", "LONG_STRANGLE"},
    }
    allowed_types = action_map.get(action)
    if not allowed_types:
        return None, "{}"

    chosen = None
    for s in structures:
        if s.structure_type in allowed_types:
            chosen = s
            break
    if not chosen:
        return None, "{}"

    params = dict(chosen.param_defaults)
    caps_by_id = caps.get(chosen.structure_id, {})
    caps_by_type = caps.get(chosen.structure_type, {})
    params.update(caps_by_type if isinstance(caps_by_type, dict) else {})
    params.update(caps_by_id if isinstance(caps_by_id, dict) else {})

    legs_spec = {
        "structure_id": chosen.structure_id,
        "structure_type": chosen.structure_type,
        "legs_template": chosen.legs_template,
        "params": params,
    }
    return chosen, json.dumps(legs_spec, ensure_ascii=False)


def _score_structure_candidates(
    base_row: pd.Series,
    candidates: List[StructureDef],
    model_bundle: dict,
) -> Tuple[StructureDef | None, float]:
    if not candidates:
        return None, 0.0
    model = model_bundle.get("model")
    columns = model_bundle.get("columns")
    if model is None or not columns:
        return None, 0.0

    base_df = pd.DataFrame([base_row]).reset_index(drop=True)
    struct_df = pd.DataFrame(
        {
            "structure_id": [c.structure_id for c in candidates],
            "structure_type": [c.structure_type for c in candidates],
        }
    )
    struct_dummies = pd.get_dummies(struct_df, columns=["structure_id", "structure_type"], prefix=["sid", "stype"])

    base_rep = pd.concat([base_df] * len(candidates), ignore_index=True)
    X = pd.concat([base_rep, struct_dummies], axis=1)
    X = X.select_dtypes(include=[np.number]).fillna(0.0)

    for col in columns:
        if col not in X.columns:
            X[col] = 0.0
    X = X[columns]
    preds = model.predict(X)
    best_idx = int(np.argmax(preds))
    return candidates[best_idx], float(preds[best_idx])


def run_once() -> None:
    # 1. System: Start of cycle
    start_time = time.time()
    system_logger.info(f"--- Cycle Start ---")
    
    ctrl = _load_controls()
    if not ctrl:
        system_logger.warning("No controls loaded. Aborting.")
        return
    
    system_logger.info(f"Config: model={ctrl.active_model_version}, universe_size={len(ctrl.universe)}")

    if not ctrl.enabled:
        logger.info("Option ML strategy disabled in controls; skipping tick")
        trader_logger.info("Strategy is currently DISABLED in controls.")
        return
    if ctrl.event_blacklist_dates:
        today = datetime.now().date().isoformat()
        if today in ctrl.event_blacklist_dates:
            logger.info("Trading disabled due to event blackout", date=today)
            trader_logger.info(f"Trading HALTED: Event Blackout for {today}")
            return

    # 2. System: Data Fetching
    t0 = time.time()
    df = _fetch_latest_features(ctrl.universe, allow_stale=ctrl.allow_stale_features)
    fetch_time = time.time() - t0
    system_logger.info(f"Data Fetch: shape={df.shape}, latency={fetch_time:.3f}s")
    
    if df.empty:
        if _is_rth():
            msg = "No features fetched during RTH."
            logger.warning(msg)
            system_logger.warning(msg)
        else:
            system_logger.info("No features fetched (outside RTH).")
        return

    X, symbols = _prepare_X(df)
    if X.empty:
        logger.warning("Feature matrix empty")
        system_logger.warning("Feature matrix X is empty after preparation.")
        return

    # 3. System: Model Loading
    try:
        clf_dir, clf_move = _load_models(ctrl.active_model_version)
        structure_ranker = _load_structure_ranker(ctrl.structure_model_version)
        system_logger.info("Models loaded successfully.")
    except Exception as e:
        logger.error("Failed to load models", error=str(e))
        system_logger.error(f"Model Load Failed: {e}")
        trader_logger.error("Critical: Models failed to load. No trading.")
        return

    # Direction probabilities
    X = _align_features(X, clf_dir)
    proba_dir = clf_dir.predict_proba(X)
    classes_dir = clf_dir.classes_
    class_to_idx = {int(c): i for i, c in enumerate(classes_dir)}
    p_up = proba_dir[:, class_to_idx.get(1, 0)]
    p_down = proba_dir[:, class_to_idx.get(-1, 0)]
    p_flat = proba_dir[:, class_to_idx.get(0, 0)]

    # Move probabilities
    proba_move = clf_move.predict_proba(X)
    classes_move = clf_move.classes_
    idx_move1 = int(np.where(classes_move == 1)[0][0]) if 1 in classes_move else 1
    p_move = proba_move[:, idx_move1]

    # Decide actions
    actions: List[str] = []
    reason_codes: List[str] = []
    
    # 4. Trader & System: Analysis
    trader_logger.info(f"Analyzing {len(symbols)} symbols...")
    
    for i in range(len(symbols)):
        sym = symbols[i]
        dir_diff = p_up[i] - p_down[i]
        dir_max = max(p_up[i], p_down[i])
        pm = p_move[i]
        
        action = "NO_TRADE"
        reason = "UNKNOWN"
        
        # Logic
        if pm < ctrl.theta_move_credit:
            if abs(dir_diff) <= ctrl.max_dir_diff_neutral:
                action = "NEUTRAL"
                reason = "SIGNAL_NEUTRAL"
            elif dir_diff >= ctrl.min_dir_diff_credit:
                action = "BULLISH_CREDIT"
                reason = "SIGNAL_BULL_CREDIT"
            elif -dir_diff >= ctrl.min_dir_diff_credit:
                action = "BEARISH_CREDIT"
                reason = "SIGNAL_BEAR_CREDIT"
            else:
                action = "NO_TRADE"
                reason = "NO_TRADE_LOW_PROB_CREDIT"
        else:
            if pm < ctrl.theta_move or dir_max < ctrl.theta_dir:
                action = "NO_TRADE"
                reason = "NO_TRADE_LOW_PROB"
            elif dir_diff >= ctrl.min_dir_diff:
                action = "BUY_CALL"
                reason = "SIGNAL_CALL"
            elif -dir_diff >= ctrl.min_dir_diff:
                action = "BUY_PUT"
                reason = "SIGNAL_PUT"
            else:
                action = "LONG_VOL_NEUTRAL"
                reason = "SIGNAL_LONG_VOL"
        
        actions.append(action)
        reason_codes.append(reason)
        
        # System Log: Detailed stats for every symbol
        system_logger.info(f"  {sym}: Action={action} | P_up={p_up[i]:.2f} P_down={p_down[i]:.2f} P_move={pm:.2f} | Diff={dir_diff:.2f}")
        
        # Trader Log: Only interesting events
        if action != "NO_TRADE":
            direction = "BULLISH" if "CALL" in action or "BULL" in action else "BEARISH" if "PUT" in action or "BEAR" in action else "NEUTRAL"
            trader_logger.info(f"  >> SIGNAL FOUND: {sym} -> {action}")
            trader_logger.info(f"     Reasoning: {direction} view. Move Prob ({pm:.2f}) > Threshold. Dir Diff ({dir_diff:.2f})")

    structures = _load_structure_catalog(ctrl.allowed_structures)
    structure_by_symbol: Dict[str, StructureDef] = {}
    legs_specs: List[str] = []
    edge_scores: List[float] = []
    
    for i, sym in enumerate(symbols):
        if actions[i] == "NO_TRADE":
            edge_scores.append(0.0)
            legs_specs.append("{}")
            continue

        base_edge = float(p_move[i] * max(p_up[i], p_down[i]))
        edge = base_edge
        
        structure, legs_spec = _select_structure_for_symbol(actions[i], structures, ctrl.structure_param_caps)
        
        # Re-ranking logic
        if structure_ranker:
            cand = []
            for s in structures:
                chosen, _ = _select_structure_for_symbol(actions[i], [s], ctrl.structure_param_caps)
                if chosen:
                    cand.append(s)
            
            best, score = _score_structure_candidates(X.iloc[i], cand, structure_ranker)
            if best:
                structure = best
                edge = score
                legs_spec = _select_structure_for_symbol(actions[i], [best], ctrl.structure_param_caps)[1]
                system_logger.info(f"  {sym} Structure Re-rank: {structure.structure_id} score={score:.3f}")

        edge_scores.append(edge)
        if structure:
            structure_by_symbol[sym] = structure
        else:
            system_logger.warning(f"  {sym}: No structure definition found for {actions[i]}")
            
        legs_specs.append(legs_spec)

    ranked_indices = sorted(
        [i for i, a in enumerate(actions) if a != "NO_TRADE"],
        key=lambda i: edge_scores[i],
        reverse=True,
    )
    ranks = [0] * len(symbols)
    top_n = max(ctrl.top_n, 0)
    
    # 5. Trader & System: Final Selection
    system_logger.info(f"Ranking Top {top_n} candidates:")
    
    for rank, idx in enumerate(ranked_indices[:top_n], start=1):
        ranks[idx] = rank
        sym = symbols[idx]
        act = actions[idx]
        struct = structure_by_symbol.get(sym)
        struct_name = struct.structure_id if struct else "Unknown"
        
        system_logger.info(f"  Rank {rank}: {sym} {act} (Score: {edge_scores[idx]:.3f})")
        trader_logger.info(f"  ** SELECTED: {sym} **")
        trader_logger.info(f"     Strategy: {act} using {struct_name}")
        
    for idx in ranked_indices[top_n:]:
        dropped_sym = symbols[idx]
        system_logger.info(f"  Dropped: {dropped_sym} (Rank {len(ranked_indices)}) - below Top {top_n}")
        trader_logger.info(f"  -- Dropped {dropped_sym} (Better opportunities available)")
        actions[idx] = "NO_TRADE"
        edge_scores[idx] = 0.0
        ranks[idx] = 0
        legs_specs[idx] = "{}"
        structure_by_symbol.pop(symbols[idx], None)
        reason_codes[idx] = "NO_TRADE_RANK"

    tags_list: List[str] = []
    for i in range(len(symbols)):
        tags = {
            "structure_model_version": ctrl.structure_model_version,
            "allow_stale_features": ctrl.allow_stale_features,
        }
        tags_list.append(json.dumps(tags))

    _write_signals(
        df["ts"],
        symbols,
        actions,
        p_up,
        p_down,
        p_flat,
        p_move,
        structure_by_symbol,
        legs_specs,
        edge_scores,
        ranks,
        reason_codes,
        tags_list,
        ctrl,
    )
    
    total = len(actions)
    buy_call = sum(1 for a in actions if a == "BUY_CALL")
    buy_put = sum(1 for a in actions if a == "BUY_PUT")
    top_n_used = sum(1 for r in ranks if r > 0)
    
    elapsed = time.time() - start_time
    summary = f"Cycle done in {elapsed:.2f}s. Signals: Call={buy_call}, Put={buy_put}, Selected={top_n_used}."
    
    system_logger.info(summary)
    if top_n_used > 0:
        trader_logger.info("Cycle Complete. Signals sent to Execution Engine.\n")
    else:
        trader_logger.info("Cycle Complete. No actionable signals found.\n")


def main() -> None:
    """
    Simple loop runner. In production, integrate with your scheduler / service.
    """
    while True:
        try:
            run_once()
        except Exception as exc:
            logger.error("Error in ml_intraday_signal_service loop", error=str(exc))
        time.sleep(60.0)


if __name__ == "__main__":
    main()
