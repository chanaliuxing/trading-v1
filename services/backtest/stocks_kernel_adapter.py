from __future__ import annotations

import csv
import asyncio
import json
import inspect
import math
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from libs.logger import get_logger
from services.backtest.backtest_engine import BacktestConfig, BacktestEngine
from strategies.stocks_kernel.builder import build_strategy_extras, build_strategy_blueprint
from strategies.stocks_kernel.templates import default_template_registry
try:
    from libs.db.ch_pool import get_client
except Exception:  # pragma: no cover - optional runtime dependency
    get_client = None  # type: ignore

logger = get_logger(__name__)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _equity_curve_values(rows: Iterable[Mapping[str, Any]]) -> List[float]:
    values: List[float] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = row.get("equity", row.get("value", row.get("nav")))
        if value is None:
            continue
        values.append(_safe_float(value, 0.0))
    return values


def _max_drawdown_pct(values: List[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    worst_dd = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            worst_dd = max(worst_dd, dd)
    return round(worst_dd * 100.0, 4)


def _simple_sharpe(values: List[float]) -> float:
    if len(values) < 3:
        return 0.0
    returns: List[float] = []
    for prev, cur in zip(values[:-1], values[1:]):
        if prev <= 0:
            continue
        returns.append((cur - prev) / prev)
    if len(returns) < 2:
        return 0.0
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std <= 0:
        return 0.0
    return round(mean_r / std, 6)


class StocksKernelBacktestAdapter:
    adapter_name = "stocks_kernel_backtest"

    def __init__(
        self,
        *,
        runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        strategy_factory: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        artifacts_root: str | Path = "runs/ai_trader/experiments",
    ) -> None:
        self.runner = runner
        self.strategy_factory = strategy_factory
        self.artifacts_root = Path(artifacts_root)

    def validate(self, spec: dict) -> dict:
        normalized, issues = self._normalize_spec(spec)
        return {
            "ok": not issues,
            "issues": issues,
            "normalized_spec": normalized,
        }

    def run(self, spec: dict) -> dict:
        validation = self.validate(spec)
        if not validation["ok"]:
            raise ValueError(f"Invalid stocks kernel experiment spec: {validation['issues']}")

        normalized = validation["normalized_spec"]

        if self.runner is not None:
            result = self.runner(normalized)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            if not isinstance(result, dict):
                raise TypeError("runner must return a dict")
            raw_result = {
                **result,
                "status": result.get("status") or "completed",
                "normalized_spec": normalized,
                "adapter": self.adapter_name,
                "source": "runner",
            }
            self._persist_artifacts(normalized, raw_result)
            return raw_result

        if self.strategy_factory is not None:
            result = self.strategy_factory(normalized)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            if not isinstance(result, dict):
                raise TypeError("strategy_factory must return a dict")
            raw_result = {
                **result,
                "status": result.get("status") or "completed",
                "normalized_spec": normalized,
                "adapter": self.adapter_name,
                "source": "strategy_factory",
            }
            self._persist_artifacts(normalized, raw_result)
            return raw_result

        engine_result = self._run_backtest_engine(normalized)
        if engine_result is not None:
            raw_result = {
                **engine_result,
                "normalized_spec": normalized,
                "adapter": self.adapter_name,
                "source": "backtest_engine",
            }
            self._persist_artifacts(normalized, raw_result)
            return raw_result

        raw_result = {
            "status": "blocked",
            "reason": "no_runner_or_engine_configured",
            "error": "no_runner_or_engine_configured",
            "normalized_spec": normalized,
            "adapter": self.adapter_name,
            "source": "placeholder",
            "metrics": {
                "final_nav": 0.0,
                "cash": 0.0,
                "total_fee": 0.0,
                "realized_pnl": 0.0,
                "trades": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
            },
            "ledger": [],
            "snapshot": {"equity": 0.0, "cash": 0.0, "positions": {}},
            "bars_processed": 0,
            "equity_curve": [],
            "trades": [],
        }
        self._persist_artifacts(normalized, raw_result)
        return raw_result

    def summarize(self, raw_result: dict) -> dict:
        spec = _as_dict(raw_result.get("normalized_spec") or raw_result.get("spec"))
        metrics = _as_dict(raw_result.get("metrics"))
        ledger = list(raw_result.get("ledger") or [])
        trades = list(raw_result.get("trades") or [])
        equity_curve = list(raw_result.get("equity_curve") or [])
        snapshot = _as_dict(raw_result.get("snapshot"))

        bars_processed = int(raw_result.get("bars_processed") or 0)
        if not bars_processed and isinstance(raw_result.get("data"), list):
            bars_processed = len(raw_result["data"])

        realized_pnl = _safe_float(metrics.get("realized_pnl"), 0.0)
        final_nav = _safe_float(metrics.get("final_nav"), 0.0)
        if not final_nav and snapshot:
            final_nav = _safe_float(snapshot.get("equity"), 0.0)

        win_rate = _safe_float(metrics.get("win_rate"), 0.0)
        profit_factor = _safe_float(metrics.get("profit_factor"), 0.0)
        total_fee = _safe_float(metrics.get("total_fee"), 0.0)

        trade_count = int(
            metrics.get("trades")
            or len(trades)
            or sum(1 for row in ledger if _safe_float(row.get("qty"), 0.0) > 0.0)
        )
        if trade_count and "win_rate" not in metrics:
            wins = sum(1 for row in trades if _safe_float(row.get("realized_pnl"), 0.0) > 0.0)
            losses = sum(1 for row in trades if _safe_float(row.get("realized_pnl"), 0.0) < 0.0)
            total = wins + losses
            win_rate = round(wins / total, 6) if total else 0.0
        if trade_count and "profit_factor" not in metrics:
            gross_profit = sum(max(_safe_float(row.get("realized_pnl"), 0.0), 0.0) for row in trades)
            gross_loss = sum(-min(_safe_float(row.get("realized_pnl"), 0.0), 0.0) for row in trades)
            profit_factor = round(gross_profit / gross_loss, 6) if gross_loss > 0 else 0.0

        equity_values = _equity_curve_values(equity_curve)
        max_drawdown_pct = _safe_float(metrics.get("max_drawdown_pct"), 0.0)
        if not max_drawdown_pct:
            max_drawdown_pct = _max_drawdown_pct(equity_values)

        sharpe = _safe_float(metrics.get("sharpe"), 0.0)
        if not sharpe and equity_values:
            sharpe = _simple_sharpe(equity_values)

        acceptance = _as_dict(spec.get("acceptance"))
        min_trades = int(acceptance.get("min_trades") or 0)
        max_dd_allowed = _safe_float(acceptance.get("max_drawdown_pct"), 100.0)

        status = str(raw_result.get("status") or "completed")
        data_ok = status in {"completed", "ok"} and bars_processed > 0
        enough_trades = trade_count >= min_trades
        passed_acceptance = bool(data_ok and enough_trades and max_drawdown_pct <= max_dd_allowed)

        execution_reality = self._build_execution_reality(spec, raw_result, status=status)
        artifacts = self._build_artifacts(spec)
        net_pnl = realized_pnl if "realized_pnl" in metrics else (final_nav - _safe_float(spec.get("starting_cash"), 0.0))

        notes = list(raw_result.get("notes") or [])
        if not notes and status != "completed":
            reason = raw_result.get("reason")
            if reason:
                notes = [str(reason)]

        result_payload = {
            "experiment_id": spec.get("experiment_id") or raw_result.get("experiment_id"),
            "cycle_id": spec.get("cycle_id") or raw_result.get("cycle_id"),
            "status": status,
            "ok": status == "completed",
            "market": spec.get("market") or "stocks",
            "strategy_family": spec.get("strategy_family") or "unknown",
            "adapter": self.adapter_name,
            "summary_metrics": {
                "trades": trade_count,
                "win_rate": round(win_rate, 6),
                "net_pnl": round(net_pnl, 6),
                "sharpe": round(sharpe, 6),
                "max_drawdown_pct": round(max_drawdown_pct, 6),
                "profit_factor": round(profit_factor, 6),
                "bars_processed": bars_processed,
                "final_nav": round(final_nav, 6),
                "total_fee": round(total_fee, 6),
            },
            "quality_flags": {
                "data_ok": data_ok,
                "enough_trades": enough_trades,
                "passed_acceptance": passed_acceptance,
            },
            "robustness": _as_dict(raw_result.get("robustness") or spec.get("robustness")),
            "execution_reality": execution_reality,
            "sizing": _as_dict(spec.get("sizing")),
            "lineage": _as_dict(spec.get("lineage")),
            "artifacts": artifacts,
            "notes": notes,
        }
        self._persist_artifacts(spec, raw_result, summary=result_payload)
        return result_payload

    def _normalize_spec(self, spec: dict) -> tuple[dict, list[str]]:
        issues: list[str] = []
        if not isinstance(spec, dict):
            return {}, ["spec_must_be_dict"]

        normalized = dict(spec)
        engine = _as_dict(normalized.get("engine"))
        universe = _as_dict(normalized.get("universe"))
        strategy_ref = _as_dict(normalized.get("strategy_ref"))
        module_selection = _as_dict(normalized.get("module_selection"))
        data_window = _as_dict(normalized.get("data_window"))
        labels_and_ml = _as_dict(normalized.get("labels_and_ml"))
        robustness = _as_dict(normalized.get("robustness"))
        execution_reality = _as_dict(normalized.get("execution_reality"))
        sizing = _as_dict(normalized.get("sizing"))
        lineage = _as_dict(normalized.get("lineage"))
        acceptance = _as_dict(normalized.get("acceptance"))

        experiment_id = str(normalized.get("experiment_id") or "").strip()
        cycle_id = str(normalized.get("cycle_id") or "").strip()
        market = str(normalized.get("market") or "").strip().lower()
        strategy_family = str(normalized.get("strategy_family") or "").strip()
        adapter = str(engine.get("adapter") or normalized.get("adapter") or "").strip()
        mode = str(engine.get("mode") or "backtest").strip().lower()
        symbols = [str(symbol).strip().upper() for symbol in _as_list(universe.get("symbols")) if str(symbol).strip()]
        template_id = str(strategy_ref.get("template_id") or "").strip()
        definition_id = strategy_ref.get("definition_id")
        features = [str(item).strip() for item in _as_list(module_selection.get("features")) if str(item).strip()]
        gates = [str(item).strip() for item in _as_list(module_selection.get("gates")) if str(item).strip()]
        parameters = _as_dict(normalized.get("parameters"))
        start = str(data_window.get("start") or "").strip()
        end = str(data_window.get("end") or "").strip()

        template_registry = default_template_registry()
        known_templates = set(template_registry.known_template_ids())
        if market != "stocks":
            issues.append("market_must_be_stocks")
        if adapter != self.adapter_name:
            issues.append("engine.adapter_must_be_stocks_kernel_backtest")
        if not experiment_id:
            issues.append("experiment_id_is_required")
        if not cycle_id:
            issues.append("cycle_id_is_required")
        if not strategy_family:
            issues.append("strategy_family_is_required")
        if not template_id:
            issues.append("strategy_ref.template_id_is_required")
        elif template_id not in known_templates:
            issues.append(f"unknown_strategy_template:{template_id}")
        if not symbols:
            issues.append("universe.symbols_is_required")
        if not start or not end:
            issues.append("data_window.start_and_end_are_required")
        if not features:
            issues.append("module_selection.features_is_required")
        if not gates:
            issues.append("module_selection.gates_is_required")

        normalized["experiment_id"] = experiment_id
        normalized["cycle_id"] = cycle_id
        normalized["market"] = "stocks"
        normalized["strategy_family"] = strategy_family
        normalized["engine"] = {
            **engine,
            "adapter": self.adapter_name,
            "mode": mode or "backtest",
        }
        normalized["universe"] = {
            **universe,
            "symbols": symbols,
        }
        normalized["strategy_ref"] = {
            "template_id": template_id,
            "definition_id": definition_id,
        }
        normalized["module_selection"] = {
            **module_selection,
            "features": features,
            "gates": gates,
        }
        normalized["parameters"] = parameters
        normalized["data_window"] = {
            **data_window,
            "start": start,
            "end": end,
            "session": str(data_window.get("session") or "rth").strip() or "rth",
        }
        normalized["labels_and_ml"] = labels_and_ml
        normalized["robustness"] = robustness
        normalized["execution_reality"] = execution_reality
        normalized["sizing"] = sizing
        normalized["lineage"] = {
            "family_id": str(lineage.get("family_id") or f"{template_id}:{strategy_family}").strip(),
            "parent_experiment_id": lineage.get("parent_experiment_id"),
            "mutation_type": str(lineage.get("mutation_type") or "seed").strip() or "seed",
            "promoted_from": lineage.get("promoted_from"),
            "rejected_reason": lineage.get("rejected_reason"),
        }
        normalized["acceptance"] = {
            "min_trades": int(acceptance.get("min_trades") or 0),
            "max_drawdown_pct": _safe_float(acceptance.get("max_drawdown_pct"), 100.0),
        }
        normalized["artifact_root"] = str(
            normalized.get("artifact_root")
            or self.artifacts_root / (experiment_id or f"{template_id}_{symbols[0] if symbols else 'unknown'}")
        )
        normalized["artifacts"] = _as_dict(normalized.get("artifacts"))

        return normalized, issues

    def _run_backtest_engine(self, normalized: dict) -> Optional[dict]:
        engine = _as_dict(normalized.get("engine"))
        backtest_cfg = _as_dict(engine.get("backtest"))
        strategy_path = str(engine.get("strategy_path") or backtest_cfg.get("strategy_path") or "").strip()
        strategy_class = str(engine.get("strategy_class") or backtest_cfg.get("strategy_class") or "").strip()
        if not strategy_path:
            strategy_path = "strategies/stocks_kernel_strategy.py"
        if not strategy_class:
            strategy_class = "StocksKernelStrategy"

        data = backtest_cfg.get("data")
        csv_path = str(backtest_cfg.get("csv") or "").strip()
        auto_data = str(os.getenv("AI_TRADER_BACKTEST_AUTO_DATA", "0")).strip().lower() in {"1", "true", "yes", "on"}
        if auto_data and data is None and not csv_path:
            data = self._load_backtest_rows_from_clickhouse(normalized)
        if not strategy_path or not strategy_class or (data is None and not csv_path):
            return None

        strategy_extras = self._build_strategy_runtime_extras(normalized)
        data_symbols = self._required_data_symbols(normalized)

        cfg = BacktestConfig(
            symbols=data_symbols,
            start=str(normalized["data_window"]["start"]),
            end=str(normalized["data_window"]["end"]),
            data=data,
            csv=csv_path or None,
            strategy_path=strategy_path,
            strategy_class=strategy_class,
            strategy_params=strategy_extras,
            starting_cash=_safe_float(backtest_cfg.get("starting_cash"), 1_000_000.0),
            commission_per_share=_safe_float(backtest_cfg.get("commission_per_share"), 0.005),
            commission_min=_safe_float(backtest_cfg.get("commission_min"), 1.0),
            slippage_bps=_safe_float(backtest_cfg.get("slippage_bps"), 1.0),
            dynamic_spread_bps=_safe_float(backtest_cfg.get("dynamic_spread_bps"), 0.0),
            borrow_rate=_safe_float(backtest_cfg.get("borrow_rate"), 0.0),
            participation_cap=_safe_float(backtest_cfg.get("participation_cap"), 0.1),
            fill_ref=str(backtest_cfg.get("fill_ref") or "open"),
            extras={
                "strategy_template": normalized["strategy_ref"]["template_id"],
                "order_namespace": normalized["strategy_ref"]["template_id"],
            },
            run_id=normalized["experiment_id"],
        )
        engine_instance = BacktestEngine(cfg)
        result = self._run_coro_safely(engine_instance.run())
        if not isinstance(result, dict):
            raise TypeError("BacktestEngine.run() must return a dict")
        return result

    def _run_coro_safely(self, coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        out: Dict[str, Any] = {}
        err: Dict[str, Exception] = {}

        def _runner() -> None:
            try:
                out["value"] = asyncio.run(coro)
            except Exception as exc:  # pragma: no cover - defensive
                err["error"] = exc

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        if "error" in err:
            raise err["error"]
        return out.get("value")

    def _load_backtest_rows_from_clickhouse(self, normalized: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
        if get_client is None:
            return None
        symbols = self._required_data_symbols(normalized)
        if not symbols:
            return None
        data_window = _as_dict(normalized.get("data_window"))
        start = str(data_window.get("start") or "").strip()
        end = str(data_window.get("end") or "").strip()
        if not start or not end:
            return None
        try:
            client = get_client()
            rows: List[Dict[str, Any]] = []
            for symbol in symbols:
                query = """
                    SELECT
                        ts_end AS ts,
                        symbol,
                        open,
                        high,
                        low,
                        close,
                        volume
                    FROM trading.bars_1m_ibkr
                    WHERE symbol = %(symbol)s
                      AND ts_end >= toDateTime(%(start)s)
                      AND ts_end <= toDateTime(%(end)s)
                    ORDER BY ts_end ASC
                    LIMIT 20000
                """
                result = client.query(
                    query,
                    {"symbol": symbol, "start": start, "end": end},
                    settings={"use_query_cache": False},
                )
                for row in result or []:
                    if not isinstance(row, Mapping):
                        continue
                    rows.append(
                        {
                            "ts": row.get("ts"),
                            "symbol": str(row.get("symbol") or symbol),
                            "open": _safe_float(row.get("open"), 0.0),
                            "high": _safe_float(row.get("high"), 0.0),
                            "low": _safe_float(row.get("low"), 0.0),
                            "close": _safe_float(row.get("close"), 0.0),
                            "volume": _safe_float(row.get("volume"), 0.0),
                        }
                    )
            rows = self._apply_backtest_symbol_proxies(normalized, rows)
            return rows or None
        except Exception as exc:
            logger.warning("stocks_kernel_backtest_data_autoload_failed", error=str(exc))
            return None

    def _apply_backtest_symbol_proxies(self, normalized: Mapping[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        parameters = _as_dict(normalized.get("parameters"))
        universe = _as_dict(normalized.get("universe"))
        signal_symbols = [str(s).strip().upper() for s in _as_list(universe.get("symbols")) if str(s).strip()]
        signal_symbol = signal_symbols[0] if signal_symbols else ""
        if not signal_symbol:
            return rows

        by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            by_symbol.setdefault(symbol, []).append(row)
        signal_rows = list(by_symbol.get(signal_symbol) or [])
        if not signal_rows:
            return rows

        exec_proxy_mode = str(parameters.get("backtest_exec_proxy_mode") or "signal_clone").strip().lower()
        if exec_proxy_mode == "signal_clone":
            for key in ("long_exec_symbol", "short_exec_symbol"):
                exec_symbol = str(parameters.get(key) or "").strip().upper()
                if exec_symbol and exec_symbol not in by_symbol:
                    rows.extend(self._clone_symbol_rows(signal_rows, target_symbol=exec_symbol, source_symbol=signal_symbol))
                    logger.warning(
                        "stocks_kernel_backtest_proxy_symbol_applied",
                        missing_symbol=exec_symbol,
                        source_symbol=signal_symbol,
                        proxy_mode=exec_proxy_mode,
                    )

        confirm_proxy_mode = str(parameters.get("backtest_confirm_proxy_mode") or "none").strip().lower()
        confirm_symbol = str(parameters.get("confirm_symbol") or "").strip().upper()
        if confirm_proxy_mode == "signal_clone" and confirm_symbol and confirm_symbol not in by_symbol:
            rows.extend(self._clone_symbol_rows(signal_rows, target_symbol=confirm_symbol, source_symbol=signal_symbol))
            logger.warning(
                "stocks_kernel_backtest_proxy_symbol_applied",
                missing_symbol=confirm_symbol,
                source_symbol=signal_symbol,
                proxy_mode=confirm_proxy_mode,
            )
        return rows

    @staticmethod
    def _clone_symbol_rows(source_rows: List[Dict[str, Any]], *, target_symbol: str, source_symbol: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in source_rows:
            cloned = dict(row)
            cloned["symbol"] = target_symbol
            cloned["_proxy_source_symbol"] = source_symbol
            out.append(cloned)
        return out

    def _build_strategy_runtime_extras(self, normalized: Mapping[str, Any]) -> Dict[str, Any]:
        parameters = _as_dict(normalized.get("parameters"))
        engine = _as_dict(normalized.get("engine"))
        backtest_cfg = _as_dict(engine.get("backtest"))
        universe = _as_dict(normalized.get("universe"))
        symbols = [str(s).strip().upper() for s in _as_list(universe.get("symbols")) if str(s).strip()]
        signal_symbol = symbols[0] if symbols else ""
        confirm_symbol = str(parameters.get("confirm_symbol") or "").strip().upper() or None
        volatility_symbol = str(parameters.get("volatility_symbol") or "").strip().upper() or None
        long_exec_symbol = str(parameters.get("long_exec_symbol") or signal_symbol).strip().upper()
        short_exec_symbol = str(parameters.get("short_exec_symbol") or long_exec_symbol).strip().upper()
        template_id = str(_as_dict(normalized.get("strategy_ref")).get("template_id") or "").strip()
        module_selection = _as_dict(normalized.get("module_selection"))
        policy_id = str(parameters.get("policy_id") or "").strip()
        dry_run = bool(backtest_cfg.get("dry_run", False))

        reserved_keys = {
            "policy_id",
            "confirm_symbol",
            "volatility_symbol",
            "long_exec_symbol",
            "short_exec_symbol",
            "backtest_exec_proxy_mode",
            "backtest_confirm_proxy_mode",
        }
        policy_params = {key: value for key, value in parameters.items() if key not in reserved_keys}
        blueprint = build_strategy_blueprint(
            strategy_template=template_id,
            signal_symbol=signal_symbol,
            long_exec_symbol=long_exec_symbol,
            short_exec_symbol=short_exec_symbol,
            confirm_symbol=confirm_symbol,
            volatility_symbol=volatility_symbol,
            dry_run=dry_run,
            order_namespace=str(normalized.get("experiment_id") or template_id).strip() or "stkv1",
            startup_preflight_enabled=False,
            feature_params={},
            gate_overrides={},
            policy_params=policy_params,
            execution_profile={},
            extra_extras={
                "feature_set": [str(item).strip() for item in _as_list(module_selection.get("features")) if str(item).strip()],
                "gate_pipeline": [str(item).strip() for item in _as_list(module_selection.get("gates")) if str(item).strip()],
                "definition_id": _as_dict(normalized.get("strategy_ref")).get("definition_id"),
                "labels_and_ml": _as_dict(normalized.get("labels_and_ml")),
                "sizing": _as_dict(normalized.get("sizing")),
                "lineage": _as_dict(normalized.get("lineage")),
                "backtest_spec": dict(normalized),
            },
        )
        extras = build_strategy_extras(blueprint)
        if policy_id:
            extras["policy_id"] = policy_id
        return extras

    def _required_data_symbols(self, normalized: Mapping[str, Any]) -> List[str]:
        parameters = _as_dict(normalized.get("parameters"))
        universe = _as_dict(normalized.get("universe"))
        symbols: List[str] = []
        for value in list(_as_list(universe.get("symbols"))) + [
            parameters.get("confirm_symbol"),
            parameters.get("volatility_symbol"),
            parameters.get("long_exec_symbol"),
            parameters.get("short_exec_symbol"),
        ]:
            symbol = str(value or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def _build_execution_reality(self, spec: dict, raw_result: dict, *, status: str) -> dict:
        execution = _as_dict(spec.get("execution_reality"))
        engine = _as_dict(spec.get("engine"))
        backtest = _as_dict(engine.get("backtest"))
        modeled_slippage_bps = _safe_float(
            execution.get("modeled_slippage_bps"),
            _safe_float(backtest.get("slippage_bps"), 0.0),
        )
        spread_cost_bps = _safe_float(execution.get("spread_cost_bps"), 0.0)
        fill_ratio = _safe_float(execution.get("fill_ratio"), 1.0 if status == "completed" else 0.0)
        partial_fill_rate = _safe_float(execution.get("partial_fill_rate"), 0.0)
        latency_bucket = str(execution.get("latency_bucket") or ("simulated" if status == "completed" else "unknown"))
        return {
            "cost_model_id": str(execution.get("cost_model_id") or "default_cost_v1"),
            "fill_model_id": str(execution.get("fill_model_id") or "paper_fill_v1"),
            "latency_model_id": str(execution.get("latency_model_id") or "baseline_latency_v1"),
            "modeled_slippage_bps": modeled_slippage_bps,
            "spread_cost_bps": spread_cost_bps,
            "fill_ratio": fill_ratio,
            "partial_fill_rate": partial_fill_rate,
            "latency_bucket": latency_bucket,
        }

    def _build_artifacts(self, spec: dict) -> dict:
        experiment_id = str(spec.get("experiment_id") or "unknown").strip() or "unknown"
        artifact_root = Path(str(spec.get("artifact_root") or self.artifacts_root / experiment_id))
        return {
            "artifact_root": str(artifact_root),
            "trade_log": str(artifact_root / "trades.jsonl"),
            "equity_curve": str(artifact_root / "equity_curve.csv"),
            "result_json": str(artifact_root / "result.json"),
            "report": str(artifact_root / "report.json"),
            "raw_result": str(artifact_root / "raw_result.json"),
        }

    def _persist_artifacts(self, spec: Mapping[str, Any], raw_result: Mapping[str, Any], summary: Optional[Mapping[str, Any]] = None) -> None:
        artifacts = self._build_artifacts(dict(spec))
        artifact_root = Path(artifacts["artifact_root"])
        artifact_root.mkdir(parents=True, exist_ok=True)

        def _write_json(path: str, payload: Any) -> None:
            Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

        def _write_jsonl(path: str, rows: Iterable[Any]) -> None:
            with Path(path).open("w", encoding="utf-8") as fh:
                for row in rows:
                    if isinstance(row, Mapping):
                        fh.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")
                    else:
                        fh.write(json.dumps({"value": row}, ensure_ascii=False, default=str) + "\n")

        def _write_csv(path: str, rows: Iterable[Any]) -> None:
            normalized_rows = [row for row in rows if isinstance(row, Mapping)]
            if not normalized_rows:
                Path(path).write_text("", encoding="utf-8")
                return
            fieldnames = sorted({key for row in normalized_rows for key in row.keys()})
            with Path(path).open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                for row in normalized_rows:
                    writer.writerow({key: row.get(key) for key in fieldnames})

        _write_json(artifacts["raw_result"], raw_result)
        _write_json(artifacts["report"], summary or raw_result)
        if summary is not None:
            _write_json(artifacts["result_json"], summary)

        trade_rows = raw_result.get("trades") or raw_result.get("ledger") or []
        _write_jsonl(artifacts["trade_log"], trade_rows)
        _write_csv(artifacts["equity_curve"], raw_result.get("equity_curve") or [])


def run_strategy_experiment(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Scheduler-facing entrypoint for stocks strategy experiments."""
    adapter = StocksKernelBacktestAdapter()
    raw_result = adapter.run(dict(spec))
    if isinstance(raw_result, dict) and raw_result.get("summary_metrics") and raw_result.get("quality_flags"):
        return raw_result
    return adapter.summarize(dict(raw_result))


__all__ = [
    "StocksKernelBacktestAdapter",
    "run_strategy_experiment",
]
