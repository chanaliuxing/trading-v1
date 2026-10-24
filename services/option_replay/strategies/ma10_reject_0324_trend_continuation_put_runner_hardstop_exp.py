"""Replay-only variant: keep baseline exits, extend hard stop only for runner."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from services.option_replay.strategies.ma10_reject_0324 import _safe_float
from services.option_replay.strategies.ma10_reject_0324_trend_continuation_put_exp import (
    MA10Reject0324TrendContinuationPutExperimentalStrategy,
)


class MA10Reject0324TrendContinuationPutRunnerHardstopExperimentalStrategy(
    MA10Reject0324TrendContinuationPutExperimentalStrategy
):
    """Keep baseline entry/soft-stop logic; extend hard stop for partial runner only."""

    def manage_position(self, engine: "object", idx: int, row: pd.Series) -> bool:
        if engine.position is None:
            return False
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        quote = engine._mark_quote(ts, engine.position.expiry, engine.position.strike, "C" if engine.position.side == "CALL" else "P")
        if quote is None:
            if ts.time() >= datetime.strptime(engine.cfg.final_flatten, "%H:%M").time():
                fallback = engine._fallback_exit_quote(ts, engine.position.side)
                if fallback is not None:
                    engine._close_position(ts, fallback, _safe_float(row.get("close")), engine.position.remaining, "EOD_FLATTEN_STALE_QUOTE")
                    return True
                engine._record_skip(ts, "missing_exit_quote_unresolved")
            return False
        engine.position.last_quote = quote

        close_px = _safe_float(row.get("close"))
        high_px = _safe_float(row.get("high"))
        low_px = _safe_float(row.get("low"))
        ma10 = _safe_float(row.get("ma10"))
        hold_bars = idx - engine.position.entry_idx

        if engine.position.side == "CALL":
            fav_move_r = (high_px - engine.position.entry_underlying) / max(engine.position.r_per_share, 1e-9)
        else:
            fav_move_r = (engine.position.entry_underlying - low_px) / max(engine.position.r_per_share, 1e-9)
        if fav_move_r > engine.position.best_fav_move_r:
            engine.position.best_fav_move_r = fav_move_r
            engine.position.best_fav_idx = idx

        if ts.time() >= datetime.strptime(engine.cfg.final_flatten, "%H:%M").time():
            engine._close_position(ts, quote, close_px, engine.position.remaining, "EOD_FLATTEN")
            return True

        if engine.position.side == "CALL" and low_px <= engine.position.structure_stop:
            engine._close_position(ts, quote, max(low_px, engine.position.structure_stop), engine.position.remaining, "STRUCTURE_STOP")
            return True
        if engine.position.side == "PUT" and high_px >= engine.position.structure_stop:
            engine._close_position(ts, quote, min(high_px, engine.position.structure_stop), engine.position.remaining, "STRUCTURE_STOP")
            return True

        # Keep baseline soft-stop behavior.
        if hold_bars >= 5:
            if engine.position.best_fav_move_r < 0.3 or (idx - engine.position.best_fav_idx) >= 5:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "SOFT_TIME_STOP")
                return True

        # Extend hard stop only after partial is taken (runner phase).
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

