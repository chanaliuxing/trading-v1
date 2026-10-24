from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import pandas as pd

from services.option_replay.core.audit import build_run_result
from services.option_replay.core.engine import SpyOptionReplayBacktester
from services.option_replay.core.market_data import load_option_quotes_for_replay, load_spy_bars
from services.option_replay.core.types import ContractQuote, ReplayConfig
from services.option_replay.strategies import get_strategy, list_strategies
from strategies.options_kernel.modules.registry import definition_registry


DEFAULT_OUTPUT_ROOT = Path("runs/ai_trader/experiments/options_single_leg_replay")
DEFAULT_REPLAY_STRATEGY_MAP: Dict[str, str] = {
    "QQQ_INTRADAY_OPTION_PARITY_V1": "orb",
    "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1": "vwap",
    "QQQ_MA10_REJECT_0324": "ma10_reject_0324",
    "QQQ_MA_CROSS_STACK_CONT_V2_DUAL": "ma_cross_stack_cont_v2_dual_exp",
    "SPY_MA_CROSS_STACK_CONT_V2_DUAL": "ma_cross_stack_cont_v2_dual_exp",
}


def _safe_str(value: Any, default: str = "") -> str:
    out = str(value or "").strip()
    return out or default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if pd.isna(out):
            return default
        return out
    except Exception:
        return default


def _to_dataframe(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, pd.Series):
        return value.to_frame().T.reset_index(drop=True)
    if isinstance(value, (list, tuple)):
        return pd.DataFrame(list(value))
    if isinstance(value, Mapping):
        return pd.DataFrame([dict(value)])
    return pd.DataFrame(value)


def _coerce_quote(value: Any) -> ContractQuote:
    if isinstance(value, ContractQuote):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"unsupported quote payload type: {type(value)!r}")
    ts = pd.Timestamp(value["ts"]).to_pydatetime().replace(second=0, microsecond=0)
    return ContractQuote(
        ts=ts,
        expiry=_safe_str(value.get("expiry")),
        right=_safe_str(value.get("right")).upper(),
        strike=_safe_float(value.get("strike")),
        dte=_safe_int(value.get("dte")),
        bid=_safe_float(value.get("bid")),
        ask=_safe_float(value.get("ask")),
        mid=_safe_float(value.get("mid")),
        iv=_safe_float(value.get("iv")),
        delta=_safe_float(value.get("delta")),
        gamma=_safe_float(value.get("gamma")),
        theta=_safe_float(value.get("theta")),
        vega=_safe_float(value.get("vega")),
        open_interest=_safe_int(value.get("open_interest")),
    )


def _coerce_option_cache(raw_cache: Any) -> Dict[Tuple[datetime, str], list[ContractQuote]]:
    if not raw_cache:
        return {}
    cache: Dict[Tuple[datetime, str], list[ContractQuote]] = {}
    for key, quotes in dict(raw_cache).items():
        if not isinstance(key, tuple) or len(key) != 2:
            raise ValueError("option_cache keys must be (timestamp, right) tuples")
        ts_raw, right_raw = key
        ts = pd.Timestamp(ts_raw).to_pydatetime().replace(second=0, microsecond=0)
        right = _safe_str(right_raw).upper()
        cache[(ts, right)] = [_coerce_quote(q) for q in (quotes or [])]
    return cache


def _extract_spec(spec: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = spec
    for key in keys:
        if not isinstance(cur, Mapping):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _json_dump(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _persist_frame(path: Path, frame: Any) -> None:
    df = _to_dataframe(frame)
    path.parent.mkdir(parents=True, exist_ok=True)
    if df.empty:
        path.write_text("", encoding="utf-8")
        return
    df.to_csv(path, index=False)


def _latency_bucket(avg_latency_sec: float | None) -> str:
    if avg_latency_sec is None:
        return "unknown"
    if avg_latency_sec < 1.0:
        return "sub_1s"
    if avg_latency_sec < 2.0:
        return "sub_2s"
    if avg_latency_sec < 5.0:
        return "sub_5s"
    if avg_latency_sec < 10.0:
        return "sub_10s"
    return "10s_plus"


@dataclass
class OptionReplayResearchAdapter:
    adapter_name: str = "options_single_leg_replay"
    output_root: Path = DEFAULT_OUTPUT_ROOT

    def validate(self, spec: Mapping[str, Any]) -> Dict[str, Any]:
        issues: list[str] = []
        definition_id = _safe_str(_extract_spec(spec, "strategy_ref", "definition_id") or spec.get("definition_id"))
        if not definition_id:
            issues.append("definition_id_missing")

        definition = None
        if definition_id:
            try:
                if definition_registry.has(definition_id):
                    definition = definition_registry.get(definition_id)
                else:
                    issues.append(f"unknown_definition_id:{definition_id}")
            except Exception as exc:
                issues.append(f"definition_lookup_failed:{exc}")

        replay_strategy_id, replay_source = self._resolve_replay_strategy_id(spec, definition)
        known_strategies = set(list_strategies())
        if not replay_strategy_id:
            issues.append("replay_strategy_id_missing")
        elif replay_strategy_id not in known_strategies:
            issues.append(f"unknown_replay_strategy_id:{replay_strategy_id}")

        underlying = _safe_str(_extract_spec(spec, "universe", "underlying") or spec.get("underlying"))
        if definition is not None and underlying and underlying.upper() != _safe_str(definition.underlying).upper():
            issues.append(
                f"underlying_mismatch:{underlying.upper()}!={_safe_str(definition.underlying).upper()}"
            )

        engine_adapter = _safe_str(_extract_spec(spec, "engine", "adapter") or spec.get("engine_adapter"))
        if engine_adapter and engine_adapter not in {self.adapter_name, "option_replay_single_leg", "option_single_leg_replay"}:
            issues.append(f"engine_adapter_mismatch:{engine_adapter}")

        return {
            "ok": not issues,
            "issues": issues,
            "definition_id": definition_id,
            "replay_strategy_id": replay_strategy_id,
            "replay_strategy_source": replay_source,
            "underlying": underlying or (_safe_str(definition.underlying) if definition else ""),
            "risk_symbol": _safe_str(definition.risk_symbol) if definition else "",
            "audit_namespace": _safe_str(definition.audit_namespace) if definition else "",
            "strategy_family": _safe_str(definition.strategy_family) if definition else "",
            "strategy_archetype": _safe_str(definition.strategy_archetype) if definition else "",
        }

    def run(self, spec: Mapping[str, Any]) -> Dict[str, Any]:
        validation = self.validate(spec)
        experiment_id = _safe_str(spec.get("experiment_id")) or self._default_experiment_id(validation["definition_id"])
        cycle_id = _safe_str(spec.get("cycle_id")) or _safe_str(_extract_spec(spec, "cycle", "cycle_id"))
        output_root = Path(spec.get("output_root") or self.output_root).expanduser()
        run_dir = output_root / experiment_id
        run_dir.mkdir(parents=True, exist_ok=True)

        if not validation["ok"]:
            result = self._build_failure_result(
                spec=spec,
                validation=validation,
                experiment_id=experiment_id,
                cycle_id=cycle_id,
                run_dir=run_dir,
                error="validation_failed",
            )
            self._persist_result(result, run_dir)
            return result

        try:
            raw_result, metadata, raw_frames = self._execute_replay(spec, validation, experiment_id, run_dir)
            result = self.summarize(
                raw_result,
                spec=spec,
                validation=validation,
                metadata=metadata,
                experiment_id=experiment_id,
                cycle_id=cycle_id,
                run_dir=run_dir,
            )
        except Exception as exc:
            result = self._build_failure_result(
                spec=spec,
                validation=validation,
                experiment_id=experiment_id,
                cycle_id=cycle_id,
                run_dir=run_dir,
                error=str(exc),
            )
            raw_frames = {}

        self._persist_result(result, run_dir, raw_frames=raw_frames if isinstance(raw_frames, Mapping) else {})
        return result

    def summarize(
        self,
        raw_result: Mapping[str, Any],
        *,
        spec: Mapping[str, Any],
        validation: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
        experiment_id: str,
        cycle_id: str,
        run_dir: Path,
    ) -> Dict[str, Any]:
        metadata = dict(metadata or {})
        summary = dict(raw_result.get("summary") or {})
        acceptance = dict(_extract_spec(spec, "acceptance", default={}) or {})
        trades = _safe_int(summary.get("trades"))
        max_drawdown_pct = _safe_float(summary.get("max_drawdown_pct"))
        min_trades = _safe_int(acceptance.get("min_trades"), 1)
        max_drawdown_cap = _safe_float(acceptance.get("max_drawdown_pct"), 999.0)

        execution_reality = self._build_execution_reality(raw_result)
        quality_flags = {
            "validation_ok": bool(validation.get("ok")),
            "data_ok": bool(summary),
            "has_fills": _safe_int(self._frame_len(raw_result.get("fills"))) > 0,
            "enough_trades": trades >= min_trades,
            "passed_acceptance": trades >= min_trades and max_drawdown_pct <= max_drawdown_cap and bool(validation.get("ok")),
        }

        normalized = {
            "experiment_id": experiment_id,
            "cycle_id": cycle_id,
            "status": "completed",
            "market": "options",
            "strategy_family": _safe_str(validation.get("strategy_family")),
            "strategy_archetype": _safe_str(validation.get("strategy_archetype")),
            "definition_id": _safe_str(validation.get("definition_id")),
            "replay_strategy_id": _safe_str(validation.get("replay_strategy_id")),
            "replay_strategy_source": _safe_str(validation.get("replay_strategy_source")),
            "underlying": _safe_str(validation.get("underlying")),
            "risk_symbol": _safe_str(validation.get("risk_symbol")),
            "audit_namespace": _safe_str(validation.get("audit_namespace")),
            "adapter": self.adapter_name,
            "data_mode": _safe_str(metadata.get("data_mode")),
            "summary_metrics": {
                "trades": trades,
                "exit_legs": _safe_int(summary.get("exit_legs")),
                "entry_count": _safe_int(summary.get("entry_count")),
                "realized_pnl": _safe_float(summary.get("realized_pnl")),
                "win_rate": _safe_float(summary.get("win_rate")),
                "round_trip_win_rate": _safe_float(summary.get("round_trip_win_rate")),
                "avg_pnl_per_entry": _safe_float(summary.get("avg_pnl_per_entry")),
                "avg_legs_per_entry": _safe_float(summary.get("avg_legs_per_entry")),
                "max_drawdown_pct": max_drawdown_pct,
                "final_cash": _safe_float(summary.get("final_cash")),
                "skipped_signals": _safe_int(summary.get("skipped_signals")),
            },
            "quality_flags": quality_flags,
            "robustness": dict(_extract_spec(spec, "robustness", default={}) or {}),
            "execution_reality": execution_reality,
            "sizing": dict(_extract_spec(spec, "sizing", default={}) or {}),
            "lineage": self._build_lineage(spec, validation),
            "artifacts": self._artifact_paths(run_dir),
            "validation": dict(validation),
            "notes": [
                "options_single_leg_replay adapter normalization",
            ],
        }
        normalized["status"] = "completed" if quality_flags["passed_acceptance"] else "degraded"
        return normalized

    def _execute_replay(
        self,
        spec: Mapping[str, Any],
        validation: Mapping[str, Any],
        experiment_id: str,
        run_dir: Path,
    ) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        inputs = dict(_extract_spec(spec, "inputs", default={}) or {})
        bars = inputs.get("bars", spec.get("bars"))
        option_cache = inputs.get("option_cache", spec.get("option_cache"))
        quote_mode = _safe_str(inputs.get("quote_mode") or spec.get("quote_mode"), "auto")
        backfill_mode = _safe_str(inputs.get("backfill_mode") or spec.get("backfill_mode"), "none")
        backfill_csv_dir = inputs.get("backfill_csv_dir") or spec.get("backfill_csv_dir")

        if bars is None:
            start_date = _safe_str(_extract_spec(spec, "data_window", "start") or spec.get("start_date"))
            end_date = _safe_str(_extract_spec(spec, "data_window", "end") or spec.get("end_date"))
            if not start_date or not end_date:
                raise ValueError("bars_missing_and_date_window_incomplete")
            bars = load_spy_bars(
                start_date,
                end_date,
                symbol=_safe_str(validation.get("underlying")) or "QQQ",
                session_mode=_safe_str(inputs.get("session_mode") or spec.get("session_mode"), "rth"),
                quality_mode=_safe_str(inputs.get("quality_mode") or spec.get("quality_mode"), "raw"),
            )

        bars_df = self._normalize_bars(bars)
        if bars_df.empty:
            raise ValueError("no_bars_available_for_replay")

        if option_cache is None:
            start_date = _safe_str(_extract_spec(spec, "data_window", "start") or spec.get("start_date"))
            end_date = _safe_str(_extract_spec(spec, "data_window", "end") or spec.get("end_date"))
            if not start_date or not end_date:
                raise ValueError("option_cache_missing_and_date_window_incomplete")
            option_cache, data_info = load_option_quotes_for_replay(
                start_date,
                end_date,
                underlying=_safe_str(validation.get("underlying")) or "QQQ",
                bars=bars_df,
                quote_mode=quote_mode,
                backfill_mode=backfill_mode,
                backfill_csv_dir=_safe_str(backfill_csv_dir),
            )
        else:
            option_cache = _coerce_option_cache(option_cache)
            data_info = {
                "data_mode": "provided",
                "confidence": "high",
                "source": "spec.inputs.option_cache",
                "backfill_attempted": False,
                "backfill_mode": "none",
            }

        replay_strategy = get_strategy(_safe_str(validation.get("replay_strategy_id")))
        replay_cfg = self._build_replay_config(spec)
        trade_start_date = _safe_str(_extract_spec(spec, "data_window", "start") or spec.get("trade_start_date"))
        backtester = SpyOptionReplayBacktester(
            bars=bars_df,
            option_cache=option_cache,
            config=replay_cfg,
            strategy=replay_strategy,
            trade_start_date=trade_start_date or None,
        )
        raw_result = backtester.run()
        metadata = dict(data_info)
        metadata["experiment_id"] = experiment_id
        metadata["run_dir"] = str(run_dir)
        metadata["trade_start_date"] = trade_start_date
        metadata["bars_rows"] = int(len(bars_df))
        metadata["option_cache_snapshots"] = int(len(option_cache))
        raw_frames = {key: raw_result.get(key) for key in raw_result.keys() if isinstance(raw_result.get(key), pd.DataFrame)}
        return raw_result, metadata, raw_frames

    def _build_replay_config(self, spec: Mapping[str, Any]) -> ReplayConfig:
        overrides = dict(_extract_spec(spec, "replay_config", default={}) or {})
        if not overrides:
            overrides = dict(_extract_spec(spec, "config", default={}) or {})
        return ReplayConfig.from_dict(overrides)

    def _build_execution_reality(self, raw_result: Mapping[str, Any]) -> Dict[str, Any]:
        orders = _to_dataframe(raw_result.get("orders"))
        fills = _to_dataframe(raw_result.get("fills"))
        selection_audit = _to_dataframe(raw_result.get("selection_audit"))

        created_orders = orders.iloc[0:0]
        if not orders.empty and "status" in orders.columns:
            created_orders = orders[orders["status"].astype(str) == "CREATED"]
        fill_order_ids = set()
        if not fills.empty and "order_id" in fills.columns:
            fill_order_ids = {str(v) for v in fills["order_id"].dropna().astype(str).tolist()}
        created_order_ids = set()
        if not created_orders.empty and "order_id" in created_orders.columns:
            created_order_ids = {str(v) for v in created_orders["order_id"].dropna().astype(str).tolist()}

        fill_ratio = (len(fill_order_ids) / len(created_order_ids)) if created_order_ids else 0.0
        fill_ratio = min(1.0, float(fill_ratio))
        partial_fill_rate = 0.0
        if not orders.empty and "status" in orders.columns and created_order_ids:
            partial_fill_rate = float(
                len(
                    orders[
                        orders["status"].astype(str).isin(["PARTIAL_FILLED", "PARTIAL"])
                        & orders["order_id"].astype(str).isin(created_order_ids)
                    ]
                )
                / len(created_order_ids)
            )

        avg_latency_sec: float | None = None
        if not fills.empty and not orders.empty and "ts" in fills.columns and "ts" in orders.columns and "order_id" in fills.columns and "order_id" in orders.columns:
            created_ts = (
                orders[orders["status"].astype(str) == "CREATED"]
                .assign(ts_parsed=lambda d: pd.to_datetime(d["ts"], errors="coerce"))
                .groupby("order_id")["ts_parsed"]
                .min()
            )
            fill_ts = pd.to_datetime(fills["ts"], errors="coerce")
            latencies: list[float] = []
            for idx, row in enumerate(fills.itertuples(index=False), start=0):
                order_id = str(getattr(row, "order_id", "") or "")
                if not order_id or order_id not in created_ts.index:
                    continue
                created = created_ts.get(order_id)
                current = fill_ts.iloc[idx]
                if pd.isna(created) or pd.isna(current):
                    continue
                latencies.append(max(0.0, (current - created).total_seconds()))
            if latencies:
                avg_latency_sec = float(sum(latencies) / len(latencies))

        spread_costs: list[float] = []
        if not selection_audit.empty and "top_candidates" in selection_audit.columns:
            for _, row in selection_audit.iterrows():
                if not bool(row.get("selected")):
                    continue
                candidates = row.get("top_candidates") or []
                if isinstance(candidates, str):
                    try:
                        candidates = json.loads(candidates)
                    except Exception:
                        candidates = []
                if not isinstance(candidates, Sequence) or not candidates:
                    continue
                first = candidates[0]
                if isinstance(first, Mapping):
                    spread_pct = _safe_float(first.get("spread_pct"))
                    if spread_pct > 0:
                        spread_costs.append(spread_pct * 10_000.0)

        slippage_bps: list[float] = []
        if not fills.empty and not selection_audit.empty:
            selected_rows = selection_audit.iloc[0:0]
            if "selected" in selection_audit.columns:
                selected_rows = selection_audit[selection_audit["selected"].astype(bool)]
            selected_rows = selected_rows.sort_values("ts") if not selected_rows.empty and "ts" in selected_rows.columns else selected_rows
            fills_sorted = fills.sort_values("ts") if "ts" in fills.columns else fills
            pairs = min(len(selected_rows), len(fills_sorted))
            for i in range(pairs):
                selected = selected_rows.iloc[i]
                candidates = selected.get("top_candidates") or []
                if isinstance(candidates, str):
                    try:
                        candidates = json.loads(candidates)
                    except Exception:
                        candidates = []
                if not isinstance(candidates, Sequence) or not candidates:
                    continue
                first = candidates[0]
                if not isinstance(first, Mapping):
                    continue
                mid = _safe_float(first.get("mid"))
                fill_price = _safe_float(fills_sorted.iloc[i].get("fill_price"))
                if mid > 0:
                    slippage_bps.append(abs(fill_price - mid) / mid * 10_000.0)

        summary = {
            "modeled_slippage_bps": float(sum(slippage_bps) / len(slippage_bps)) if slippage_bps else 0.0,
            "spread_cost_bps": float(sum(spread_costs) / len(spread_costs)) if spread_costs else 0.0,
            "fill_ratio": float(fill_ratio),
            "partial_fill_rate": float(partial_fill_rate),
            "latency_bucket": _latency_bucket(avg_latency_sec),
            "avg_fill_latency_sec": avg_latency_sec,
            "order_count": _safe_int(len(created_orders)),
            "fill_count": _safe_int(len(fills)),
            "selection_count": _safe_int(len(selection_audit)),
        }
        return summary

    def _build_failure_result(
        self,
        *,
        spec: Mapping[str, Any],
        validation: Mapping[str, Any],
        experiment_id: str,
        cycle_id: str,
        run_dir: Path,
        error: str,
    ) -> Dict[str, Any]:
        acceptance = dict(_extract_spec(spec, "acceptance", default={}) or {})
        result = {
            "experiment_id": experiment_id,
            "cycle_id": cycle_id,
            "status": "failed",
            "market": "options",
            "strategy_family": _safe_str(validation.get("strategy_family")),
            "strategy_archetype": _safe_str(validation.get("strategy_archetype")),
            "definition_id": _safe_str(validation.get("definition_id")),
            "replay_strategy_id": _safe_str(validation.get("replay_strategy_id")),
            "replay_strategy_source": _safe_str(validation.get("replay_strategy_source")),
            "underlying": _safe_str(validation.get("underlying")),
            "risk_symbol": _safe_str(validation.get("risk_symbol")),
            "audit_namespace": _safe_str(validation.get("audit_namespace")),
            "adapter": self.adapter_name,
            "summary_metrics": {
                "trades": 0,
                "exit_legs": 0,
                "entry_count": 0,
                "realized_pnl": 0.0,
                "win_rate": 0.0,
                "round_trip_win_rate": 0.0,
                "avg_pnl_per_entry": 0.0,
                "avg_legs_per_entry": 0.0,
                "max_drawdown_pct": 0.0,
                "final_cash": 0.0,
                "skipped_signals": 0,
            },
            "quality_flags": {
                "validation_ok": bool(validation.get("ok")),
                "data_ok": False,
                "has_fills": False,
                "enough_trades": False,
                "passed_acceptance": False,
            },
            "robustness": dict(_extract_spec(spec, "robustness", default={}) or {}),
            "execution_reality": {
                "modeled_slippage_bps": 0.0,
                "spread_cost_bps": 0.0,
                "fill_ratio": 0.0,
                "partial_fill_rate": 0.0,
                "latency_bucket": "unknown",
                "avg_fill_latency_sec": None,
                "order_count": 0,
                "fill_count": 0,
                "selection_count": 0,
            },
            "sizing": dict(_extract_spec(spec, "sizing", default={}) or {}),
            "lineage": self._build_lineage(spec, validation),
            "artifacts": self._artifact_paths(run_dir),
            "validation": dict(validation),
            "error": error,
            "notes": [
                f"acceptance.min_trades={_safe_int(acceptance.get('min_trades'), 1)}",
            ],
        }
        return result

    def _build_lineage(self, spec: Mapping[str, Any], validation: Mapping[str, Any]) -> Dict[str, Any]:
        lineage = dict(_extract_spec(spec, "lineage", default={}) or {})
        if not lineage.get("family_id"):
            lineage["family_id"] = _safe_str(validation.get("strategy_family")) or _safe_str(validation.get("definition_id"))
        lineage.setdefault("parent_experiment_id", None)
        lineage.setdefault("mutation_type", "seed")
        return lineage

    def _artifact_paths(self, run_dir: Path) -> Dict[str, str]:
        return {
            "run_dir": str(run_dir),
            "result_json": str(run_dir / "result.json"),
            "summary_json": str(run_dir / "summary.json"),
            "trades_csv": str(run_dir / "trades.csv"),
            "equity_curve_csv": str(run_dir / "equity_curve.csv"),
            "orders_csv": str(run_dir / "orders.csv"),
            "fills_csv": str(run_dir / "fills.csv"),
            "selection_audit_csv": str(run_dir / "selection_audit.csv"),
            "entry_audit_csv": str(run_dir / "entry_audit.csv"),
            "bracket_audit_csv": str(run_dir / "bracket_audit.csv"),
            "skips_csv": str(run_dir / "skips.csv"),
        }

    def _persist_result(self, result: Mapping[str, Any], run_dir: Path, *, raw_frames: Mapping[str, Any] | None = None) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts = result.get("artifacts") or {}
        json_result = dict(result)
        json_result.pop("_raw_frames", None)
        _json_dump(Path(artifacts["result_json"]), json_result)
        _json_dump(Path(artifacts["summary_json"]), {"summary_metrics": result.get("summary_metrics"), "quality_flags": result.get("quality_flags"), "execution_reality": result.get("execution_reality")})

        if isinstance(raw_frames, Mapping):
            for key, path_key in (
                ("trades", "trades_csv"),
                ("equity_curve", "equity_curve_csv"),
                ("orders", "orders_csv"),
                ("fills", "fills_csv"),
                ("selection_audit", "selection_audit_csv"),
                ("entry_audit", "entry_audit_csv"),
                ("bracket_audit", "bracket_audit_csv"),
                ("skips", "skips_csv"),
            ):
                _persist_frame(Path(artifacts[path_key]), raw_frames.get(key))

    def _default_experiment_id(self, definition_id: str) -> str:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        suffix = definition_id.lower() if definition_id else "options"
        return f"{ts}-{suffix}"

    def _normalize_bars(self, bars: Any) -> pd.DataFrame:
        df = _to_dataframe(bars)
        if df.empty:
            return df
        if "ts" not in df.columns:
            raise ValueError("bars_missing_ts_column")
        df = df.copy()
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
        df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
        if "symbol" not in df.columns:
            df["symbol"] = "QQQ"
        if "vwap" not in df.columns:
            df["vwap"] = df["close"]
        if "volume" not in df.columns:
            df["volume"] = 1.0
        return df

    def _resolve_replay_strategy_id(
        self,
        spec: Mapping[str, Any],
        definition: Any,
    ) -> tuple[str, str]:
        spec_override = _safe_str(_extract_spec(spec, "strategy_ref", "replay_strategy_id") or spec.get("replay_strategy_id"))
        if spec_override:
            return spec_override, "spec_override"

        if definition is not None:
            extra_id = _safe_str((getattr(definition, "extras", {}) or {}).get("replay_strategy_id"))
            if extra_id and extra_id in set(list_strategies()):
                return extra_id, "definition_extra"

            definition_id = _safe_str(getattr(definition, "definition_id", ""))
            fallback = DEFAULT_REPLAY_STRATEGY_MAP.get(definition_id)
            if fallback:
                return fallback, "adapter_fallback"

        definition_id = _safe_str(_extract_spec(spec, "strategy_ref", "definition_id") or spec.get("definition_id"))
        fallback = DEFAULT_REPLAY_STRATEGY_MAP.get(definition_id)
        if fallback:
            return fallback, "adapter_fallback"
        return "", "unresolved"

    def _frame_len(self, value: Any) -> int:
        if isinstance(value, pd.DataFrame):
            return len(value)
        return len(_to_dataframe(value))


def run_strategy_experiment(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Scheduler-facing entrypoint for options strategy experiments."""
    adapter = OptionReplayResearchAdapter()
    return adapter.run(dict(spec))


__all__ = [
    "OptionReplayResearchAdapter",
    "DEFAULT_OUTPUT_ROOT",
    "DEFAULT_REPLAY_STRATEGY_MAP",
    "run_strategy_experiment",
]
