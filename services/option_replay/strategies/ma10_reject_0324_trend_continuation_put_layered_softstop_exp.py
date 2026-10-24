"""Replay-only variant: layered soft time stop for trend-continuation PUT runner."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from services.option_replay.strategies.ma10_reject_0324 import _safe_float
from services.option_replay.strategies.ma10_reject_0324_trend_continuation_put_exp import (
    MA10Reject0324TrendContinuationPutExperimentalStrategy,
)


class MA10Reject0324TrendContinuationPutLayeredSoftStopExperimentalStrategy(
    MA10Reject0324TrendContinuationPutExperimentalStrategy
):
    """Replay-only light risk control: early no-progress exit on configured signal kind."""

    def manage_position(self, engine: "object", idx: int, row: pd.Series) -> bool:
        if engine.position is None:
            return False
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        quote = engine._mark_quote(ts, engine.position.expiry, engine.position.strike, "C" if engine.position.side == "CALL" else "P")
        if quote is None:
            if engine.position is not None:
                engine.position.quote_missing_bars = int(getattr(engine.position, "quote_missing_bars", 0) or 0) + 1
            force_exit_bars = int(getattr(engine.cfg, "branch_quote_missing_force_exit_bars", 0) or 0)
            if (
                force_exit_bars > 0
                and engine.position is not None
                and bool(getattr(engine.cfg, "branch_risk_control_enabled", False))
            ):
                raw_kinds = str(getattr(engine.cfg, "branch_risk_signal_kinds_csv", "") or "")
                kinds = {k.strip() for k in raw_kinds.split(",") if k.strip()}
                if kinds and engine.position.signal_kind in kinds and int(getattr(engine.position, "quote_missing_bars", 0) or 0) >= force_exit_bars:
                    fallback = engine._fallback_exit_quote(ts, engine.position.side)
                    if fallback is not None:
                        engine._close_position(ts, fallback, _safe_float(row.get("close")), engine.position.remaining, "QUOTE_MISSING_FORCE_FLATTEN")
                        return True
            if ts.time() >= datetime.strptime(engine.cfg.final_flatten, "%H:%M").time():
                fallback = engine._fallback_exit_quote(ts, engine.position.side)
                if fallback is not None:
                    engine._close_position(ts, fallback, _safe_float(row.get("close")), engine.position.remaining, "EOD_FLATTEN_STALE_QUOTE")
                    return True
                engine._record_skip(ts, "missing_exit_quote_unresolved")
            return False
        if engine.position is not None:
            engine.position.quote_missing_bars = 0
        engine.position.last_quote = quote

        close_px = _safe_float(row.get("close"))
        high_px = _safe_float(row.get("high"))
        low_px = _safe_float(row.get("low"))
        ma10 = _safe_float(row.get("ma10"))
        close_pos = _safe_float(row.get("close_pos"), 0.5)
        hold_bars = idx - engine.position.entry_idx

        if engine.position.side == "CALL":
            fav_move_r = (high_px - engine.position.entry_underlying) / max(engine.position.r_per_share, 1e-9)
        else:
            fav_move_r = (engine.position.entry_underlying - low_px) / max(engine.position.r_per_share, 1e-9)
        if fav_move_r > engine.position.best_fav_move_r:
            engine.position.best_fav_move_r = fav_move_r
            engine.position.best_fav_idx = idx

        # Targeted oracle fail-fast: exit only early bars when adverse expansion is high
        # and favorable progress remains weak.
        if bool(getattr(engine.cfg, "oracle_failfast_enabled", False)):
            raw = str(getattr(engine.cfg, "oracle_failfast_signal_kinds_csv", "") or "")
            kinds = {k.strip() for k in raw.split(",") if k.strip()}
            if kinds and engine.position.signal_kind in kinds:
                eval_max = int(getattr(engine.cfg, "oracle_failfast_eval_bars_max", 4) or 0)
                if eval_max > 0 and hold_bars <= eval_max:
                    adverse_r = 0.0
                    if engine.position.side == "PUT":
                        adverse_r = (high_px - engine.position.entry_underlying) / max(engine.position.r_per_share, 1e-9)
                        min_close_pos = float(getattr(engine.cfg, "oracle_failfast_min_close_pos_put", 0.70) or 0.0)
                        close_reversal = close_pos >= min_close_pos
                    else:
                        adverse_r = (engine.position.entry_underlying - low_px) / max(engine.position.r_per_share, 1e-9)
                        max_close_pos = float(getattr(engine.cfg, "oracle_failfast_max_close_pos_call", 0.30) or 1.0)
                        close_reversal = close_pos <= max_close_pos
                    min_adverse = float(getattr(engine.cfg, "oracle_failfast_min_adverse_r", 0.75) or 0.0)
                    max_fav = float(getattr(engine.cfg, "oracle_failfast_max_fav_r", 0.15) or 0.0)
                    if adverse_r >= min_adverse and engine.position.best_fav_move_r <= max_fav and close_reversal:
                        engine._close_position(ts, quote, close_px, engine.position.remaining, "ORACLE_FAILFAST")
                        return True

        if ts.time() >= datetime.strptime(engine.cfg.final_flatten, "%H:%M").time():
            engine._close_position(ts, quote, close_px, engine.position.remaining, "EOD_FLATTEN")
            return True

        if engine.position.side == "CALL" and low_px <= engine.position.structure_stop:
            engine._close_position(ts, quote, max(low_px, engine.position.structure_stop), engine.position.remaining, "STRUCTURE_STOP")
            return True
        if engine.position.side == "PUT" and high_px >= engine.position.structure_stop:
            engine._close_position(ts, quote, min(high_px, engine.position.structure_stop), engine.position.remaining, "STRUCTURE_STOP")
            return True

        # Lightweight no-progress soft-stop for continuation risk control.
        # Designed for experiments: keep entry model intact, reduce bad continuation exposure.
        min_hold_bars = max(1, int(getattr(engine.cfg, "layered_softstop_min_hold_bars", 5)))
        low_progress_threshold = float(getattr(engine.cfg, "layered_softstop_min_fav_r", 0.2))
        signal_scope = str(getattr(engine.cfg, "layered_softstop_signal_kind", "trend_continuation_put") or "").strip()
        scoped_match = (not signal_scope) or (engine.position.signal_kind == signal_scope)
        if scoped_match and hold_bars >= min_hold_bars and engine.position.best_fav_move_r < low_progress_threshold:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "SOFT_TIME_STOP_NO_PROGRESS")
            return True

        # Optional branch-scoped no-progress stop (e.g., oracle fallback only).
        if bool(getattr(engine.cfg, "branch_risk_control_enabled", False)):
            raw_kinds = str(getattr(engine.cfg, "branch_risk_signal_kinds_csv", "") or "")
            kinds = {k.strip() for k in raw_kinds.split(",") if k.strip()}
            if kinds and engine.position.signal_kind in kinds:
                branch_hold = int(getattr(engine.cfg, "branch_softstop_min_hold_bars", 0) or 0)
                branch_min_fav = float(getattr(engine.cfg, "branch_softstop_min_fav_r", 0.0) or 0.0)
                if branch_hold > 0 and branch_min_fav > 0 and hold_bars >= branch_hold and engine.position.best_fav_move_r < branch_min_fav:
                    engine._close_position(ts, quote, close_px, engine.position.remaining, "SOFT_TIME_STOP_BRANCH_NO_PROGRESS")
                    return True

        # Optional branch pre-stop reduction on reversal bar (before structure stop).
        if (
            bool(getattr(engine.cfg, "branch_prestop_enabled", False))
            and (not bool(getattr(engine.position, "branch_prestop_reduced", False)))
            and engine.position.remaining >= 2
            and bool(getattr(engine.cfg, "branch_risk_control_enabled", False))
        ):
            raw_kinds = str(getattr(engine.cfg, "branch_risk_signal_kinds_csv", "") or "")
            kinds = {k.strip() for k in raw_kinds.split(",") if k.strip()}
            if kinds and engine.position.signal_kind in kinds:
                hold_min = int(getattr(engine.cfg, "branch_prestop_hold_bars_min", 0) or 0)
                hold_max = int(getattr(engine.cfg, "branch_prestop_hold_bars_max", 0) or 0)
                in_hold_window = hold_bars >= hold_min and (hold_max <= 0 or hold_bars <= hold_max)
                if in_hold_window:
                    open_px = _safe_float(row.get("open"))
                    rng = max(1e-9, high_px - low_px)
                    body_frac = abs(close_px - open_px) / rng
                    close_pos = _safe_float(row.get("close_pos"), 0.5)
                    atr = max(1e-9, _safe_float(row.get("atr")))
                    high_over_ma10_atr = (high_px - ma10) / atr if ma10 > 0 else 0.0
                    low_under_ma10_atr = (ma10 - low_px) / atr if ma10 > 0 else 0.0
                    body_min = float(getattr(engine.cfg, "branch_prestop_body_frac_min", 0.0) or 0.0)

                    reversal = False
                    if engine.position.side == "PUT":
                        close_pos_min = float(getattr(engine.cfg, "branch_prestop_close_pos_put_min", 1.0) or 1.0)
                        over_min = float(getattr(engine.cfg, "branch_prestop_high_over_ma10_atr_min", 0.0) or 0.0)
                        reversal = body_frac >= body_min and close_pos >= close_pos_min and high_over_ma10_atr >= over_min
                    else:
                        close_pos_max = float(getattr(engine.cfg, "branch_prestop_close_pos_call_max", 0.0) or 0.0)
                        under_min = float(getattr(engine.cfg, "branch_prestop_low_under_ma10_atr_min", 0.0) or 0.0)
                        reversal = body_frac >= body_min and close_pos <= close_pos_max and low_under_ma10_atr >= under_min
                    if reversal:
                        reduce_qty = max(1, engine.position.remaining // 2)
                        engine._close_position(ts, quote, close_px, reduce_qty, "BRANCH_PRESTOP_REDUCE")
                        if engine.position is not None:
                            engine.position.branch_prestop_reduced = True
                        return True

        # Keep runner extension behavior from runner-hardstop variant.
        if engine.position.contracts >= 2 and engine.position.partial_taken:
            if hold_bars >= 16:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "HARD_TIME_STOP_RUNNER_EXT")
                return True
        else:
            if hold_bars >= 10:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "HARD_TIME_STOP")
                return True

        if engine.position.contracts >= 2 and not engine.position.partial_taken:
            hit_1r = close_px >= engine.position.target_1r if engine.position.side == "CALL" else close_px <= engine.position.target_1r
            if hit_1r:
                close_qty = max(1, engine.position.remaining // 2)
                engine._close_position(ts, quote, close_px, close_qty, "TP1_1R")
                if engine.position is not None:
                    engine.position.partial_taken = True
                return True

        if engine.position.contracts == 1:
            hit_1p2r = close_px >= engine.position.target_1p2r if engine.position.side == "CALL" else close_px <= engine.position.target_1p2r
            ma10_fail = close_px < ma10 if engine.position.side == "CALL" else close_px > ma10
            if hit_1p2r:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "TP_1P2R")
                return True
            if ma10 > 0 and ma10_fail:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "MA10_FAIL")
                return True

        if engine.position.contracts >= 2 and engine.position.partial_taken and ma10 > 0:
            ma10_fail = close_px < ma10 if engine.position.side == "CALL" else close_px > ma10
            if ma10_fail:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "RUNNER_MA10_FAIL")
                return True
        return False
