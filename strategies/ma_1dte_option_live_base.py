from __future__ import annotations

import json
import os
from hashlib import sha1
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import pandas as pd

from libs.options.internal_symbol import format_internal_opt_symbol, parse_internal_opt_symbol
from services.market.options_quote_store import latest_quote_status, query_option_rows, quote_minute_coverage
from services.option_replay.core.market_data import build_features
from services.option_replay.core.selector import explain_contract_rejection, select_contract
from services.option_replay.core.types import ContractQuote, PendingSetup, ReplayConfig
from services.option_replay.strategies import get_strategy as get_replay_strategy
from strategies.option_live_parity import DecisionSnapshotStore, build_snapshot_id
from strategies.qqq_0dte_opt_live_strategy import (
    ET,
    QQQ0DTEOptionsLiveStrategy,
    _PositionState,
    _minute_floor,
    _parse_hms,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return default


def _normalize_expiry_ymd(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw.replace("-", "")


class MA1DTEMA10RejectLiveStrategyBase(QQQ0DTEOptionsLiveStrategy):
    """
    Shared live runtime for 1DTE MA option strategies.

    Strategy-specific wrappers should override the default metadata methods
    instead of subclassing each other.
    """

    def _strategy_slug(self) -> str:
        return "qqq_1dte_ma10_reject"

    def _audit_component_name(self) -> str:
        return f"{self._strategy_slug()}_live_strategy"

    def _log_event(self, suffix: str) -> str:
        return f"{self._strategy_slug()}_{suffix}"

    def _default_underlying(self) -> str:
        return "QQQ"

    def _default_risk_symbol(self) -> str:
        return "OPT:QQQ:1DTE"

    def _default_live_parity_profile_id(self) -> str:
        return "qqq_option_ma_ext_v1"

    def _default_live_parity_snapshot_root(self) -> str:
        return "runtime/state/decision_snapshots"

    def _default_replay_strategy_id(self) -> str:
        return "ma10_reject_0324"

    def _state_filename(self) -> str:
        return f"{self._strategy_slug()}_live_{self.strategy_id}.json"

    def _emit_local_audit(
        self,
        *,
        stage: str,
        severity: str = "info",
        status: Optional[str] = None,
        reason: Optional[str] = None,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        try:
            writer.emit(
                domain="strategy",
                component=self._audit_component_name(),
                stage=stage,
                severity=severity,
                strategy_id=self.strategy_id,
                strategy_run_id=strategy_run_id,
                trigger_id=str(trigger_id or "").strip() or None,
                decision_id=str(decision_id or "").strip() or None,
                trace_id=str(trace_id or "").strip() or None,
                symbol=symbol or getattr(self, "underlying", None),
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            self.ctx.log.warning(
                self._log_event("local_audit_emit_failed"),
                strategy_id=str(self.strategy_id),
                stage=stage,
                error=str(exc),
            )

    async def initialize(self) -> None:
        extras = self.params.extras or {}
        extras.setdefault("underlying", self._default_underlying())
        extras.setdefault("risk_symbol", self._default_risk_symbol())
        extras.setdefault("contracts", 4)
        extras.setdefault("ab_time_start", "09:40:00")
        extras.setdefault("ab_time_end", "15:00:00")
        extras.setdefault("no_new_entry_after", "15:00:00")
        extras.setdefault("force_flatten_time", "15:50:00")
        extras.setdefault("max_trades_per_day", 4)
        extras.setdefault("second_trade_only_if_first_bad_exit", False)
        extras.setdefault("quote_hb_gate_enabled", True)
        extras.setdefault("quote_health_min_coverage", 0.70)
        extras.setdefault("entry_quote_lookback_min", 2)
        extras.setdefault("entry_quote_max_age_sec", 90)
        extras.setdefault("mark_quote_lookback_min", 2)
        extras.setdefault("mark_quote_max_age_sec", 90)
        extras.setdefault("exit_quote_lookback_min", 3)
        extras.setdefault("exit_degraded_nbbo_max_age_sec", 5.0)
        extras.setdefault("dte_preferred", 1)
        extras.setdefault("dte_fallback_max", 2)
        extras.setdefault("delta_min", 0.45)
        extras.setdefault("delta_max", 0.55)
        extras.setdefault("delta_fallback_min", 0.40)
        extras.setdefault("delta_fallback_max", 0.60)
        extras.setdefault("min_open_interest", 100)
        extras.setdefault("spread_max_pct", 0.12)
        extras.setdefault("confirm_window_bars", 2)
        extras.setdefault("confirm_decay_from_bar", 99)
        extras.setdefault("ma10_reject_ext_atr_max", 2.2)
        extras.setdefault("enable_fresh_momentum_branch", True)
        extras.setdefault("enable_daily_best_additive_branch", True)
        extras.setdefault("daily_best_call_only", True)
        extras.setdefault("daily_best_min_dist_vwap_atr", 2.0)
        extras.setdefault("preload_history_bars", 260)
        extras.setdefault("enable_live_parity_snapshot", False)
        extras.setdefault("enable_live_shadow_mode", False)
        extras.setdefault("live_parity_profile_id", self._default_live_parity_profile_id())
        extras.setdefault("live_parity_snapshot_root", self._default_live_parity_snapshot_root())
        self.params.extras = extras

        self._replay_strategy = get_replay_strategy(self._default_replay_strategy_id())
        self._pending_replay_setup: Optional[PendingSetup] = None
        self._pending_entry_plan: Optional[Dict[str, Any]] = None
        self._active_entry_plan_for_submit: Optional[Dict[str, Any]] = None
        self._state_path = Path("runtime/state") / self._state_filename()
        self._decision_snapshot_store = DecisionSnapshotStore(
            root_dir=Path(str(extras.get("live_parity_snapshot_root") or "runtime/state/decision_snapshots"))
        )

        await super().initialize()
        self._restore_runtime_state()

    def _parity_snapshot_enabled(self) -> bool:
        return bool((self.params.extras or {}).get("enable_live_parity_snapshot", False))

    def _live_shadow_mode_enabled(self) -> bool:
        return bool((self.params.extras or {}).get("enable_live_shadow_mode", False))

    def _live_parity_profile_id(self) -> str:
        default = self._default_live_parity_profile_id()
        return str((self.params.extras or {}).get("live_parity_profile_id") or default).strip() or default

    def _entry_signal_kind(self) -> str:
        plan = self._active_entry_plan_for_submit or self._pending_entry_plan or {}
        return str(plan.get("signal_kind") or "ma10_reject")

    @staticmethod
    def _decision_snapshot_key(
        *,
        strategy_id: str,
        bar_close_ts: datetime,
        signal_kind: str,
        opt_symbol: str,
    ) -> str:
        raw = "|".join(
            [
                str(strategy_id or "").strip(),
                bar_close_ts.astimezone(ET).replace(second=0, microsecond=0).isoformat(),
                str(signal_kind or "").strip(),
                str(opt_symbol or "").strip().upper(),
            ]
        )
        return f"bar:{sha1(raw.encode('utf-8')).hexdigest()[:20]}"

    def _prepare_entry_intent(
        self,
        *,
        now: datetime,
        side: str,
        spot: float,
        entry_bar: Dict[str, Any],
        quote: Dict[str, Any],
        trigger_id: str,
        decision_id: str,
        trace_id: str,
    ) -> tuple[bool, Dict[str, Any]]:
        if not self._parity_snapshot_enabled():
            return True, {}

        opt_symbol = str(quote.get("opt_symbol") or "")
        signal_kind = self._entry_signal_kind()
        snapshot_id = build_snapshot_id(
            strategy_id=str(self.strategy_id),
            bar_close_ts=now.astimezone(ET).replace(second=0, microsecond=0),
            signal_kind=signal_kind,
            side=str(side),
            opt_symbol=opt_symbol,
        )
        dedupe_key = self._decision_snapshot_key(
            strategy_id=str(self.strategy_id),
            bar_close_ts=now,
            signal_kind=signal_kind,
            opt_symbol=opt_symbol,
        )
        if self._decision_snapshot_store.exists(strategy_id=str(self.strategy_id), ts=now, snapshot_id=snapshot_id):
            self._emit_local_audit(
                stage="entry_snapshot_duplicate_blocked",
                severity="warning",
                status="blocked",
                reason="DUPLICATE_SNAPSHOT_ID",
                symbol=opt_symbol or self.underlying,
                decision_id=decision_id,
                trigger_id=trigger_id,
                trace_id=trace_id,
                payload={
                    "snapshot_id": str(snapshot_id),
                    "dedupe_key": str(dedupe_key),
                    "signal_kind": str(signal_kind),
                    "side": str(side),
                },
            )
            return False, {}

        payload: Dict[str, Any] = {
            "snapshot_id": str(snapshot_id),
            "dedupe_key": str(dedupe_key),
            "position_id": f"pos:{str(self.strategy_id).strip()}:{str(snapshot_id)}"[:120],
            "strategy_id": str(self.strategy_id),
            "parity_profile_id": self._live_parity_profile_id(),
            "bar_close_ts": now.astimezone(ET).replace(second=0, microsecond=0).isoformat(),
            "underlying": str(self.underlying),
            "selected_contract": {
                "opt_symbol": opt_symbol,
                "strike": float(quote.get("strike") or 0.0),
                "bid": float(quote.get("bid") or 0.0),
                "ask": float(quote.get("ask") or 0.0),
                "mid": float(quote.get("mid") or 0.0),
                "last": float(quote.get("last") or 0.0),
                "quote_age_sec": float(quote.get("quote_age_sec") or 0.0),
            },
            "decision_trace": {
                "trigger_id": str(trigger_id),
                "decision_id": str(decision_id),
                "trace_id": str(trace_id),
            },
            "signal": {
                "side": str(side),
                "signal_kind": str(signal_kind),
                "spot": float(spot),
            },
            "entry_bar": {
                "vwap": _safe_float(entry_bar.get("vwap"), spot),
                "atr_1m": _safe_float(entry_bar.get("atr_1m"), 0.0),
            },
            "plan": {
                "signal_kind": str((self._active_entry_plan_for_submit or self._pending_entry_plan or {}).get("signal_kind") or signal_kind),
                "structure_stop": float((self._active_entry_plan_for_submit or self._pending_entry_plan or {}).get("structure_stop") or 0.0),
                "target_1r": float((self._active_entry_plan_for_submit or self._pending_entry_plan or {}).get("target_1r") or 0.0),
                "target_1p2r": float((self._active_entry_plan_for_submit or self._pending_entry_plan or {}).get("target_1p2r") or 0.0),
                "r_per_share": float((self._active_entry_plan_for_submit or self._pending_entry_plan or {}).get("r_per_share") or 0.0),
                "initial_contracts": int((self._active_entry_plan_for_submit or self._pending_entry_plan or {}).get("initial_contracts") or int((self.params.extras or {}).get("contracts", 4) or 4)),
            },
        }
        path = self._decision_snapshot_store.write(
            strategy_id=str(self.strategy_id),
            ts=now,
            snapshot_id=snapshot_id,
            payload=payload,
        )
        self._emit_local_audit(
            stage="entry_snapshot_written",
            status="written",
            reason="SNAPSHOT_OK",
            symbol=opt_symbol or self.underlying,
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            payload={
                "snapshot_id": str(snapshot_id),
                "dedupe_key": str(dedupe_key),
                "path": str(path),
                "parity_profile_id": self._live_parity_profile_id(),
                "signal_kind": str(signal_kind),
            },
        )
        return True, {
            "decision_snapshot_id": str(snapshot_id),
            "decision_snapshot_key": str(dedupe_key),
            "parity_profile_id": self._live_parity_profile_id(),
            "position_id": f"pos:{str(self.strategy_id).strip()}:{str(snapshot_id)}"[:120],
        }

    def _skip_entry_intent_submit(
        self,
        *,
        now: datetime,
        side: str,
        env: Any,
        quote: Dict[str, Any],
        trigger_id: str,
        decision_id: str,
        trace_id: str,
    ) -> bool:
        if not self._live_shadow_mode_enabled():
            return False
        self._emit_local_audit(
            stage="entry_shadow_mode_skipped",
            status="shadowed",
            reason="SHADOW_MODE_ENABLED",
            symbol=str(quote.get("opt_symbol") or self.underlying),
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            payload={
                "side": str(side),
                "contracts": int(env.target_qty or 0),
                "snapshot_id": str((env.extras or {}).get("decision_snapshot_id") or ""),
                "parity_profile_id": str((env.extras or {}).get("parity_profile_id") or ""),
            },
        )
        return True

    def _daily_reset(self, now: datetime) -> None:
        prev_day = self._day
        super()._daily_reset(now)
        if self._day != prev_day:
            self._pending_replay_setup = None
            self._pending_entry_plan = None
            self._replay_strategy.on_new_day()
            self._preload_history(now)
            self._persist_runtime_state(reason="daily_reset")

    def _persist_runtime_state(self, *, reason: str) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload: Dict[str, Any] = {
                "day": str(self._day or ""),
                "updated_at": datetime.now(ET).isoformat(),
                "reason": str(reason),
                "trades_filled_today": int(self._trades_filled_today or 0),
                "first_trade_exit_reason": str(self._first_trade_exit_reason or ""),
            }
            if self._pos is not None:
                payload["position"] = {
                    "opt_symbol": str(self._pos.opt_symbol),
                    "side": str(self._pos.side),
                    "entry_time": self._pos.entry_time.astimezone(ET).isoformat(),
                    "position_id": str(getattr(self._pos, "position_id", "") or ""),
                    "entry_decision_id": str(getattr(self._pos, "entry_decision_id", "") or ""),
                    "entry_trigger_id": str(getattr(self._pos, "entry_trigger_id", "") or ""),
                    "entry_snapshot_id": str(getattr(self._pos, "entry_snapshot_id", "") or ""),
                    "entry_underlying": float(getattr(self._pos, "entry_underlying", 0.0) or 0.0),
                    "entry_close": float(getattr(self._pos, "entry_close", 0.0) or 0.0),
                    "entry_vwap": float(getattr(self._pos, "entry_vwap", 0.0) or 0.0),
                    "trace_id": str(getattr(self._pos, "trace_id", "") or ""),
                    "tp1_done": bool(getattr(self._pos, "tp1_done", False)),
                    "partial_taken": bool(getattr(self._pos, "partial_taken", False)),
                    "filled": bool(getattr(self._pos, "filled", False)),
                    "signal_kind": str(getattr(self._pos, "signal_kind", "") or ""),
                    "structure_stop": float(getattr(self._pos, "structure_stop", 0.0) or 0.0),
                    "target_1r": float(getattr(self._pos, "target_1r", 0.0) or 0.0),
                    "target_1p2r": float(getattr(self._pos, "target_1p2r", 0.0) or 0.0),
                    "r_per_share": float(getattr(self._pos, "r_per_share", 0.0) or 0.0),
                    "initial_contracts": int(getattr(self._pos, "initial_contracts", 0) or 0),
                    "best_fav_move_r": float(getattr(self._pos, "best_fav_move_r", 0.0) or 0.0),
                    "best_fav_hold_bars": int(getattr(self._pos, "best_fav_hold_bars", 0) or 0),
                }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except Exception as exc:
            self.ctx.log.warning(
                self._log_event("state_persist_failed"),
                strategy_id=str(self.strategy_id),
                error=str(exc),
            )

    def _restore_runtime_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.ctx.log.warning(
                self._log_event("state_restore_failed"),
                strategy_id=str(self.strategy_id),
                error=str(exc),
            )
            return

        today = datetime.now(ET).strftime("%Y-%m-%d")
        if str(raw.get("day") or "") != today:
            return

        self._trades_filled_today = int(raw.get("trades_filled_today", 0) or 0)
        first_exit_reason = str(raw.get("first_trade_exit_reason") or "").strip()
        self._first_trade_exit_reason = first_exit_reason or None

        pos = raw.get("position")
        if not isinstance(pos, dict):
            return
        try:
            entry_time_raw = str(pos.get("entry_time") or "")
            entry_time = datetime.fromisoformat(entry_time_raw).astimezone(ET) if entry_time_raw else datetime.now(ET)
            self._pos = _PositionState(
                opt_symbol=str(pos.get("opt_symbol") or ""),
                side=str(pos.get("side") or "CALL"),
                entry_time=entry_time,
                position_id=str(pos.get("position_id") or ""),
                entry_decision_id=str(pos.get("entry_decision_id") or ""),
                entry_trigger_id=str(pos.get("entry_trigger_id") or ""),
                entry_snapshot_id=str(pos.get("entry_snapshot_id") or ""),
                entry_underlying=float(pos.get("entry_underlying") or 0.0),
                entry_close=float(pos.get("entry_close") or 0.0),
                entry_vwap=float(pos.get("entry_vwap") or 0.0),
                trace_id=str(pos.get("trace_id") or "restored"),
                tp1_done=bool(pos.get("tp1_done", False)),
                filled=bool(pos.get("filled", True)),
            )
            self._pos.partial_taken = bool(pos.get("partial_taken", False))
            self._pos.signal_kind = str(pos.get("signal_kind") or "ma10_reject")
            self._pos.structure_stop = float(pos.get("structure_stop") or 0.0)
            self._pos.target_1r = float(pos.get("target_1r") or 0.0)
            self._pos.target_1p2r = float(pos.get("target_1p2r") or 0.0)
            self._pos.r_per_share = float(pos.get("r_per_share") or 0.0)
            self._pos.initial_contracts = int(pos.get("initial_contracts") or 0)
            self._pos.best_fav_move_r = float(pos.get("best_fav_move_r") or 0.0)
            self._pos.best_fav_hold_bars = int(pos.get("best_fav_hold_bars") or 0)
            self.ctx.log.info(
                self._log_event("state_restored"),
                strategy_id=str(self.strategy_id),
                symbol=str(self._pos.opt_symbol),
                trace_id=str(self._pos.trace_id),
            )
        except Exception as exc:
            self.ctx.log.warning(
                self._log_event("state_restore_position_failed"),
                strategy_id=str(self.strategy_id),
                error=str(exc),
            )
            self._pos = None

    def _preload_history(self, now: datetime) -> None:
        bars_to_load = max(0, int((self.params.extras or {}).get("preload_history_bars", 260) or 0))
        if bars_to_load <= 0:
            return
        ch = get_client()
        session_start = datetime.combine(now.date(), time(9, 30), tzinfo=ET)
        try:
            rows = ch.query(
                """
                SELECT
                  ts_start AS ts,
                  open,
                  high,
                  low,
                  close,
                  vwap,
                  volume
                FROM trading.bars_1m_ibkr FINAL
                WHERE symbol = {s:String}
                  AND is_rth = 1
                  AND ts_start < {cutoff:DateTime64(3, 'America/New_York')}
                ORDER BY ts_start DESC
                LIMIT {n:UInt32}
                """,
                parameters={
                    "s": self.underlying,
                    "cutoff": session_start,
                    "n": int(bars_to_load),
                },
            )
        except Exception as exc:
            self.ctx.log.warning(
                self._log_event("preload_failed"),
                strategy_id=str(self.strategy_id),
                underlying=self.underlying,
                error=str(exc),
            )
            return
        if not rows:
            return
        seed_rows = list(reversed(rows))
        for row in seed_rows:
            ts = row.get("ts")
            if isinstance(ts, datetime):
                ts = ts.astimezone(ET)
            else:
                continue
            bar = {
                "ts": ts,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": _safe_float(row.get("close")),
                "vwap": _safe_float(row.get("vwap"), _safe_float(row.get("close"))),
                "volume": _safe_float(row.get("volume")),
                "atr_d": 0.0,
                "atr_1m": 0.0,
                "atr_1m_ma": 0.0,
                "vol_ma": None,
                "rvol": None,
            }
            self._enrich_bar(bar)
            self._bars.append(bar)

    def _replay_cfg(self, spread_max_pct: Optional[float] = None) -> ReplayConfig:
        extras = self.params.extras or {}
        return ReplayConfig.from_dict(
            {
                "entry_start_am": "09:40",
                "entry_end_am": "15:00",
                "entry_start_pm": "09:40",
                "entry_end_pm": "15:00",
                "final_flatten": "15:50",
                "fixed_contracts_per_trade": int(extras.get("contracts", 4) or 4),
                "confirm_window_bars": int(extras.get("confirm_window_bars", 2) or 2),
                "confirm_decay_from_bar": int(extras.get("confirm_decay_from_bar", 99) or 99),
                "confirm_decay_max_distance_atr": float(extras.get("confirm_decay_max_distance_atr", 0.30) or 0.30),
                "enable_fresh_momentum_branch": bool(extras.get("enable_fresh_momentum_branch", True)),
                "ma10_reject_ext_atr_max": float(extras.get("ma10_reject_ext_atr_max", 2.2) or 2.2),
                "enable_entry_quality_gate": False,
                "enable_daily_best_additive_branch": bool(extras.get("enable_daily_best_additive_branch", True)),
                "daily_best_call_only": bool(extras.get("daily_best_call_only", True)),
                "daily_best_min_dist_vwap_atr": float(extras.get("daily_best_min_dist_vwap_atr", 2.0) or 2.0),
                "delta_min": float(extras.get("delta_min", 0.45) or 0.45),
                "delta_max": float(extras.get("delta_max", 0.55) or 0.55),
                "delta_fallback_min": float(extras.get("delta_fallback_min", 0.40) or 0.40),
                "delta_fallback_max": float(extras.get("delta_fallback_max", 0.60) or 0.60),
                "dte_preferred": int(extras.get("dte_preferred", 1) or 1),
                "dte_fallback_max": int(extras.get("dte_fallback_max", 2) or 2),
                "min_open_interest": int(extras.get("min_open_interest", 100) or 100),
                "max_spread_pct": float(spread_max_pct if spread_max_pct is not None else extras.get("spread_max_pct", 0.12) or 0.12),
            }
        )

    def _build_feature_frame(self) -> pd.DataFrame:
        rows: List[Dict[str, Any]] = []
        for bar in self._bars:
            ts = bar.get("ts")
            if not isinstance(ts, datetime):
                continue
            rows.append(
                {
                    "ts": ts.astimezone(ET).replace(tzinfo=None),
                    "symbol": self.underlying,
                    "open": _safe_float(bar.get("open")),
                    "high": _safe_float(bar.get("high")),
                    "low": _safe_float(bar.get("low")),
                    "close": _safe_float(bar.get("close")),
                    "vwap": _safe_float(bar.get("vwap"), _safe_float(bar.get("close"))),
                    "volume": _safe_float(bar.get("volume")),
                }
            )
        if not rows:
            return pd.DataFrame()
        return build_features(pd.DataFrame(rows))

    def _entry_dte_bounds(self) -> Tuple[int, int]:
        extras = self.params.extras or {}
        preferred = max(0, int(extras.get("dte_preferred", 1) or 1))
        fallback = max(preferred, int(extras.get("dte_fallback_max", 2) or 2))
        return preferred, fallback

    def _latest_quote_age_sec(self, now: datetime) -> Optional[float]:
        status = self._latest_quote_status(now)
        if not status:
            return None
        return float(status.get("age_sec") or 0.0)

    def _latest_quote_status(self, now: datetime) -> Optional[Dict[str, Any]]:
        dte_min, dte_max = self._entry_dte_bounds()
        try:
            return latest_quote_status(
                underlying=self.underlying,
                now=now,
                day_start=datetime.combine(now.date(), time(0, 0), tzinfo=ET),
                day_end=datetime.combine(now.date() + timedelta(days=1), time(0, 0), tzinfo=ET),
                dte_min=int(dte_min),
                dte_max=int(dte_max),
            )
        except Exception:
            return None

    def _query_quote_minute_coverage(self, window_start: datetime, window_end: datetime) -> Dict[str, int]:
        dte_min, dte_max = self._entry_dte_bounds()
        return quote_minute_coverage(
            underlying=self.underlying,
            window_start=window_start,
            window_end=window_end,
            dte_min=int(dte_min),
            dte_max=int(dte_max),
        )

    def _select_0dte_quote(
        self,
        now: datetime,
        *,
        side: str,
        spot: float,
        spread_max_pct: float,
        now_for_quote_age: Optional[datetime] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        extras = self.params.extras or {}
        right = "C" if side == "CALL" else "P"
        ts = _minute_floor(now)
        lookback_min = max(1, int(extras.get("entry_quote_lookback_min", 2)))
        quote_max_age_sec = max(1.0, float(extras.get("entry_quote_max_age_sec", 90.0)))
        ts_lb = ts - timedelta(minutes=lookback_min)
        dte_min, dte_max = self._entry_dte_bounds()
        allow_ch_fallback = bool(extras.get("entry_quote_allow_ch_fallback", False))
        rows = query_option_rows(
            underlying=self.underlying,
            right=right,
            dte_min=int(dte_min),
            dte_max=int(dte_max),
            ts_lb=ts_lb,
            ts_now=ts,
            fallback_to_ch=allow_ch_fallback,
        )
        if not rows:
            return None, "NO_QUOTES"

        ref_now = now_for_quote_age or now
        stale_count = 0
        allow_stale_entry_quotes = bool(extras.get("allow_stale_entry_quotes", False))
        chain: List[ContractQuote] = []
        for row in rows:
            src = row.get("created_at") or row.get("ts")
            if not isinstance(src, datetime):
                stale_count += 1
                continue
            age_sec = max(0.0, (ref_now - src.astimezone(ET)).total_seconds())
            if (not allow_stale_entry_quotes) and age_sec > quote_max_age_sec:
                stale_count += 1
                continue
            quote = ContractQuote(
                ts=_minute_floor(row.get("ts").astimezone(ET) if isinstance(row.get("ts"), datetime) else now),
                expiry=str(row.get("expiry") or ""),
                right=str(row.get("right") or right),
                strike=_safe_float(row.get("strike")),
                dte=int(row.get("dte") or 0),
                bid=_safe_float(row.get("bid")),
                ask=_safe_float(row.get("ask")),
                mid=_safe_float(row.get("mid")),
                iv=_safe_float(row.get("iv")),
                delta=_safe_float(row.get("delta")),
                gamma=_safe_float(row.get("gamma")),
                theta=_safe_float(row.get("theta")),
                vega=_safe_float(row.get("vega")),
                open_interest=int(row.get("open_interest") or 0),
            )
            chain.append(quote)
        if not chain:
            return None, "STALE_QUOTE" if stale_count > 0 else "NO_VALID_QUOTE"

        cfg = self._replay_cfg(spread_max_pct=spread_max_pct)
        selected = select_contract(chain, right=right, spot=spot, aggressive_same_day=False, cfg=cfg)
        if selected is None:
            return None, explain_contract_rejection(chain, right=right, aggressive_same_day=False, cfg=cfg)

        expiry_ymd = _normalize_expiry_ymd(selected.expiry)
        opt_symbol = format_internal_opt_symbol(
            underlying=self.underlying,
            expiry_ymd=expiry_ymd,
            strike=float(selected.strike),
            right=right,
        )
        return (
            {
                "opt_symbol": opt_symbol,
                "strike": float(selected.strike),
                "bid": float(selected.bid),
                "ask": float(selected.ask),
                "mid": float(selected.mid),
                "last": float(selected.mid),
                "quote_age_sec": float(min(
                    (
                        max(
                            0.0,
                            (ref_now - (row.get("created_at") or row.get("ts")).astimezone(ET)).total_seconds(),
                        )
                        for row in rows
                        if str(row.get("expiry") or "") == str(selected.expiry)
                        and str(row.get("right") or right) == str(selected.right)
                        and abs(_safe_float(row.get("strike")) - float(selected.strike)) <= 1e-9
                        and isinstance((row.get("created_at") or row.get("ts")), datetime)
                    ),
                    default=0.0,
                )),
            },
            None,
        )

    async def _maybe_enter(self, now: datetime, bar: Dict[str, Any], wall_now: Optional[datetime] = None) -> None:
        extras = self.params.extras or {}
        wall_now = wall_now or datetime.now(ET)
        self._flush_entry_block_logs(now)
        self._update_quote_data_health_gate(now)
        self._maybe_release_quote_data_health_gate(now)

        gate_start = _parse_hms(extras.get("ab_time_start", "09:40:00"), time(9, 40))
        gate_end = _parse_hms(extras.get("ab_time_end", "15:00:00"), time(15, 0))
        if now.time() < gate_start or now.time() > gate_end:
            self._pending_replay_setup = None
            self._record_entry_block(now, "OUTSIDE_ENTRY_WINDOW")
            return

        no_new_after = _parse_hms(extras.get("no_new_entry_after", "15:00:00"), time(15, 0))
        if now.time() >= no_new_after:
            self._pending_replay_setup = None
            self._record_entry_block(now, "AFTER_NO_NEW_ENTRY")
            return

        max_trades = int(extras.get("max_trades_per_day", 4) or 4)
        if self._trades_filled_today >= max_trades:
            self._pending_replay_setup = None
            self._record_entry_block(now, "MAX_TRADES_REACHED")
            return
        if self._quote_health_block_day:
            self._pending_replay_setup = None
            self._record_entry_block(now, "DATA_HEALTH_BLOCK_DAY")
            return
        if not self._quote_heartbeat_allows_entry(now):
            self._pending_replay_setup = None
            self._record_entry_block(now, "QUOTE_HEARTBEAT_PAUSED")
            return

        df = self._build_feature_frame()
        if df.empty or len(df) < 210:
            self._record_entry_block(now, "INSUFFICIENT_WARMUP")
            return
        idx = len(df) - 1
        row = df.iloc[idx]
        cfg = self._replay_cfg(spread_max_pct=float(extras.get("spread_max_pct", 0.12) or 0.12))

        if self._pending_replay_setup is not None:
            pending = self._pending_replay_setup
            bars_since_setup = idx - int(pending.setup_idx)
            if bars_since_setup <= 0:
                return
            if bars_since_setup > max(1, int(cfg.confirm_window_bars)):
                self._pending_replay_setup = None
                self._record_entry_block(now, "CONFIRM_WINDOW_EXPIRED")
                return

            close_px = _safe_float(row.get("close"))
            high_px = _safe_float(row.get("high"))
            low_px = _safe_float(row.get("low"))
            ma10 = _safe_float(row.get("ma10"))

            if pending.side == "CALL":
                if low_px < pending.low - 1e-9:
                    self._pending_replay_setup = None
                    self._record_entry_block(now, "PENDING_INVALIDATED_SETUP_LOW")
                    return
                if ma10 > 0 and close_px <= ma10:
                    self._pending_replay_setup = None
                    self._record_entry_block(now, "PENDING_INVALIDATED_MA10")
                    return
            else:
                if high_px > pending.high + 1e-9:
                    self._pending_replay_setup = None
                    self._record_entry_block(now, "PENDING_INVALIDATED_SETUP_HIGH")
                    return
                if ma10 > 0 and close_px >= ma10:
                    self._pending_replay_setup = None
                    self._record_entry_block(now, "PENDING_INVALIDATED_MA10")
                    return

            if not self._replay_strategy.confirm_signal(df, idx, row, pending, cfg):
                self._record_entry_block(now, "CONFIRM_NOT_MET")
                return

            structure_stop, target_1r, target_1p2r = self._replay_strategy.build_position_targets(
                df,
                idx,
                row,
                pending,
                close_px,
            )
            self._pending_entry_plan = {
                "signal_kind": str(pending.signal_kind),
                "structure_stop": float(structure_stop),
                "target_1r": float(target_1r),
                "target_1p2r": float(target_1p2r),
                "r_per_share": float(abs(close_px - structure_stop)),
                "initial_contracts": int(extras.get("contracts", 4) or 4),
            }
            self.ctx.log.info(
                self._log_event("entry_confirmed"),
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self.strategy_id),
                signal_kind=str(pending.signal_kind),
                side=str(pending.side),
                regime=str(pending.regime),
                close=float(close_px),
                setup_ts=str(pending.setup_ts),
            )
            self._pending_replay_setup = None
            await self._submit_entry(
                now,
                side=str(pending.side),
                spot=float(close_px),
                entry_bar={"vwap": _safe_float(row.get("vwap_day"), close_px), "atr_1m": _safe_float(row.get("atr_14"))},
                spread_max_pct=float(extras.get("spread_max_pct", 0.12) or 0.12),
                wall_now=wall_now,
            )
            return

        signal = self._replay_strategy.detect_signal(df, idx, row, cfg)
        if signal is None:
            self._record_entry_block(now, "NO_SIGNAL")
            return
        self._pending_replay_setup = self._replay_strategy.build_pending_setup(signal, now.replace(second=0, microsecond=0), idx, row)
        self.ctx.log.info(
            self._log_event("setup_detected"),
            ts=now.strftime("%Y-%m-%d %H:%M:%S"),
            strategy_id=str(self.strategy_id),
            signal_kind=str(signal.signal_kind),
            side=str(signal.side),
            regime=str(signal.regime),
            grade=str(signal.grade),
        )

    async def _submit_entry(
        self,
        now: datetime,
        *,
        side: str,
        spot: float,
        entry_bar: Dict[str, Any],
        spread_max_pct: float,
        wall_now: Optional[datetime] = None,
        audit_ids: Optional[Tuple[str, str, str]] = None,
    ) -> None:
        plan = dict(self._pending_entry_plan or {})
        self._pending_entry_plan = None
        self._active_entry_plan_for_submit = dict(plan)
        try:
            await super()._submit_entry(
                now,
                side=side,
                spot=spot,
                entry_bar=entry_bar,
                spread_max_pct=spread_max_pct,
                wall_now=wall_now,
                audit_ids=audit_ids,
            )
        finally:
            self._active_entry_plan_for_submit = None
        if self._pos is None or not plan:
            return
        self._pos.signal_kind = str(plan.get("signal_kind") or "ma10_reject")
        self._pos.structure_stop = float(plan.get("structure_stop") or spot)
        self._pos.target_1r = float(plan.get("target_1r") or spot)
        self._pos.target_1p2r = float(plan.get("target_1p2r") or spot)
        self._pos.r_per_share = float(plan.get("r_per_share") or 0.0)
        self._pos.initial_contracts = int(plan.get("initial_contracts") or int((self.params.extras or {}).get("contracts", 4) or 4))
        self._pos.partial_taken = False
        self._pos.best_fav_move_r = 0.0
        self._pos.best_fav_hold_bars = 0
        self._persist_runtime_state(reason="entry_submitted")

    async def _sync_and_manage_position(self, now: datetime, bar: Dict[str, Any]) -> bool:
        extras = self.params.extras or {}
        if self._pos is None:
            try:
                oos = await self.ctx.open_orders()
                if self._has_strategy_owned_open_option_orders(oos):
                    return True
            except Exception:
                pass
            return False

        opt_symbol = self._pos.opt_symbol
        ps = await self.ctx.position(opt_symbol)
        qty = self._strategy_owned_qty_cap(
            opt_symbol=opt_symbol,
            account_qty=float(getattr(ps, "pos_qty", 0.0) or 0.0),
        )
        if qty <= 0:
            try:
                oos = await self.ctx.open_orders()
                if self._has_strategy_owned_open_option_orders(oos, opt_symbol=opt_symbol):
                    return True
            except Exception:
                pass
            if self._pos.filled and self._first_trade_exit_reason is None:
                self._first_trade_exit_reason = str(self._pos.last_exit_reason or "MANUAL_FLATTEN").upper()
            self._pos = None
            self._persist_runtime_state(reason="position_cleared")
            return False

        if not self._pos.filled:
            self._pos.filled = True
            self._trades_filled_today += 1

        initial_contracts = int(getattr(self._pos, "initial_contracts", int(extras.get("contracts", 4) or 4)) or 4)
        if qty <= max(1.0, float(initial_contracts - 2)):
            self._pos.partial_taken = True
            self._pos.tp1_done = True
            self._persist_runtime_state(reason="tp1_detected")

        force_flatten_time = _parse_hms(extras.get("force_flatten_time", "15:50:00"), time(15, 50))
        if now.time() >= force_flatten_time:
            await self._submit_exit(now, opt_symbol, sell_qty=int(round(qty)), reason="EOD_FORCE_FLATTEN", current_qty=qty)
            return True

        mark = self._get_mark_for_contract(now, opt_symbol)
        if mark is None or mark <= 0:
            return True

        feat_df = self._build_feature_frame()
        feat_row = feat_df.iloc[-1] if not feat_df.empty else None
        close_px = _safe_float(bar.get("close"))
        high_px = _safe_float(bar.get("high"), close_px)
        low_px = _safe_float(bar.get("low"), close_px)
        ma10 = _safe_float(feat_row.get("ma10") if feat_row is not None else 0.0, 0.0)
        entry_underlying = float(getattr(self._pos, "entry_underlying", close_px) or close_px)
        raw_r = float(getattr(self._pos, "r_per_share", 0.0) or 0.0)
        structure_stop = float(getattr(self._pos, "structure_stop", 0.0) or 0.0)
        target_1r = float(getattr(self._pos, "target_1r", 0.0) or 0.0)
        hold_bars = max(0, len(self._bars_after_entry(self._pos.entry_time)))

        # Restart-adopted positions do not necessarily have full strategy metadata.
        # In that case only protect with EOD flatten and wait for manual supervision.
        if raw_r <= 0.0 or structure_stop <= 0.0 or target_1r <= 0.0:
            self._persist_runtime_state(reason="holding_without_full_metadata")
            return True
        r_per_share = max(0.01, raw_r)

        if self._pos.side == "CALL":
            fav_move_r = (high_px - entry_underlying) / r_per_share
        else:
            fav_move_r = (entry_underlying - low_px) / r_per_share
        if fav_move_r > float(getattr(self._pos, "best_fav_move_r", 0.0) or 0.0):
            self._pos.best_fav_move_r = float(fav_move_r)
            self._pos.best_fav_hold_bars = int(hold_bars)

        if self._pos.side == "CALL" and low_px <= structure_stop:
            await self._submit_exit(now, opt_symbol, sell_qty=int(round(qty)), reason="STRUCTURE_STOP", current_qty=qty)
            return True
        if self._pos.side == "PUT" and high_px >= structure_stop:
            await self._submit_exit(now, opt_symbol, sell_qty=int(round(qty)), reason="STRUCTURE_STOP", current_qty=qty)
            return True

        if hold_bars >= 5:
            best_fav_move_r = float(getattr(self._pos, "best_fav_move_r", 0.0) or 0.0)
            best_fav_hold_bars = int(getattr(self._pos, "best_fav_hold_bars", 0) or 0)
            if best_fav_move_r < 0.3 or (hold_bars - best_fav_hold_bars) >= 5:
                await self._submit_exit(now, opt_symbol, sell_qty=int(round(qty)), reason="SOFT_TIME_STOP", current_qty=qty)
                return True

        if hold_bars >= 10:
            await self._submit_exit(now, opt_symbol, sell_qty=int(round(qty)), reason="HARD_TIME_STOP", current_qty=qty)
            return True

        if (not bool(getattr(self._pos, "partial_taken", False))) and qty >= 2:
            hit_1r = close_px >= target_1r if self._pos.side == "CALL" else close_px <= target_1r
            if hit_1r:
                sell_qty = min(2, int(round(qty)))
                submitted_qty = await self._submit_exit(now, opt_symbol, sell_qty=sell_qty, reason="TP1_1R", current_qty=qty)
                if submitted_qty > 0:
                    self._pos.partial_taken = True
                    self._pos.tp1_done = True
                    self._persist_runtime_state(reason="tp1_submitted")
                return True

        if bool(getattr(self._pos, "partial_taken", False)) and ma10 > 0:
            ma10_fail = close_px < ma10 if self._pos.side == "CALL" else close_px > ma10
            if ma10_fail:
                await self._submit_exit(now, opt_symbol, sell_qty=int(round(qty)), reason="RUNNER_MA10_FAIL", current_qty=qty)
                return True
        self._persist_runtime_state(reason="position_heartbeat")
        return True
