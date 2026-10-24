#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nightly HPO (Draft/MVP)

Generates per-symbol parameter suggestions by sampling a discrete space (see
doc/enhanced_ml_hpo_space.md) and running a lightweight scoring proxy.

MVP: does NOT execute full backtests (placeholder). Writes suggestion JSON
ready for approval and later application.
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List

import yaml


def _load_cfg() -> Dict[str, Any]:
    for fname in ("config.yml", "config.yaml", "config.example.yml"):
        p = Path(fname)
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}
    return {}


SPACE = {
    "sizing.fixed.base_qty": [50, 100, 150, 200],
    "sizing.fixed.max_qty": [500, 1000, 1500],
    "sizing.confidence_scaled.multiplier": [1.0, 1.5, 2.0, 2.5],
    "market_filter.conf_floor": [0.5, 0.55, 0.6, 0.65, 0.7],
    "market_filter.max_spread_bps": [8, 10, 12],
    "risk.take_profit_pct": [0.01, 0.015, 0.02, 0.025],
    "risk.stop_loss_pct": [0.008, 0.01, 0.012, 0.015],
    "risk.time_stop_min": [30, 45, 60, 90],
    # Ensemble weight search (normalized on apply)
    "ensemble_weight_xgb": [0.3, 0.4, 0.5, 0.6, 0.7],
    "ensemble_weight_lstm": [0.7, 0.6, 0.5, 0.4, 0.3],
}


def _enabled_symbols(cfg: Dict[str, Any]) -> List[str]:
    out = []
    for s in (cfg.get("strategies") or []):
        if s and s.get("enabled"):
            ex = ((s.get("params") or {}).get("extras")) or {}
            syms = ex.get("symbols") or ([] if not ex.get("symbol") else [ex.get("symbol")])
            out.extend([str(x).upper() for x in syms])
    return sorted(list(set(out)))


def _sample_k(space: Dict[str, List[Any]], k: int) -> List[Dict[str, Any]]:
    keys = list(space.keys())
    out = []
    for _ in range(k):
        params = {}
        for key in keys:
            params[key] = random.choice(space[key])
        out.append(params)
    return out


def _apply_params_to_extras(base: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Merge flat dotted params into extras dict for strategy."""
    import copy
    x = copy.deepcopy(base or {})
    for k, v in params.items():
        parts = k.split('.')
        cur = x
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = v
    return x


def _read_symbol_json(config_dir: str, prefix: str, sym: str) -> Dict[str, Any]:
    p = Path(config_dir) / f"{prefix}_{sym}.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description="Nightly HPO (draft)")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--samples", type=int, default=24)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--real-backtest", action="store_true", help="Run real backtests using BacktestEngine")
    ap.add_argument("--config-dir", default="strategies/configs")
    ap.add_argument("--prefix", default="enhanced_ml")
    # Walk-forward options
    ap.add_argument("--wf", action="store_true", help="Use walk-forward splits (OOS scoring)")
    ap.add_argument("--is-days", type=int, default=10)
    ap.add_argument("--oos-days", type=int, default=5)
    ap.add_argument("--gap-days", type=int, default=0)
    ap.add_argument("--step-days", type=int, default=None)
    ap.add_argument("--embargo-days", type=int, default=0)
    # Features table override for backtests
    ap.add_argument("--features-table-override", dest="features_table_override", default=None,
                    help="Override features table used by BacktestEngine (e.g., trading.features_1m_active)")
    ap.add_argument("--report-md", default=None)
    args = ap.parse_args()

    cfg = _load_cfg()
    symbols = args.symbols or _enabled_symbols(cfg)
    if not symbols:
        print(json.dumps({"ok": False, "error": "no symbols"}))
        return

    day = datetime.now().date().isoformat()
    out_dir = Path(args.out_dir or (Path("runs/ai_trader") / day / "hpo_suggestions"))
    out_dir.mkdir(parents=True, exist_ok=True)

    # Sample K configs per symbol; optionally backtest (WF/OOS)
    for sym in symbols:
        candidates = _sample_k(SPACE, args.samples)
        scored = []
        if args.real_backtest:
            # Attempt real backtest via BacktestEngine
            try:
                from backtest_ml_engine import BacktestEngine, BacktestConfig
                from wf_hpo import make_folds
                # Pick recent window
                end = datetime.now().strftime("%Y-%m-%d 16:00:00")
                start = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d 09:30:00")
                folds = None
                if args.wf:
                    folds = make_folds(start, end, is_days=args.is_days, oos_days=args.oos_days, gap_days=args.gap_days, step_days=args.step_days, embargo_days=args.embargo_days)
                base_cfg = _read_symbol_json(args.config_dir, args.prefix, sym)
                features_table = args.features_table_override or "trading.features_1m"
                import gc as _gc
                for p in candidates:
                    extras = _apply_params_to_extras(base_cfg, p)
                    try:
                        if folds:
                            oos_scores = []
                            oos_metrics = []
                            for fd in folds:
                                cfg = BacktestConfig(
                                    symbols=[sym],
                                    start=fd["oos_start"],
                                    end=fd["oos_end"],
                                    timeframe="1m",
                                    features_table=features_table,
                                    strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
                                    strategy_params={"extras": {**extras, "symbols": [sym]}},
                                    initial_cash=100000.0,
                                    commission_per_share=0.0035,
                                    commission_min=1.0,
                                    slippage_bps=1.0,
                                    fill_ref="open",
                                )
                                eng = BacktestEngine(cfg)
                                result = eng.run()
                                met = result.get("metrics", {})
                                # Drop heavy result keys to limit memory
                                try:
                                    result.pop("trades", None)
                                    result.pop("equity_curve", None)
                                    result.pop("final_snapshot", None)
                                except Exception:
                                    pass
                                del result
                                _gc.collect()
                                # Composite: OOS total_return penalized by DD, PF floor
                                score = float(met.get("total_return", 0.0)) * 100.0
                                score -= max(0.0, (met.get("max_drawdown", 0.0) - 0.03)) * 100.0
                                if met.get("profit_factor") and met.get("profit_factor") < 1.1:
                                    score -= 10.0
                                oos_scores.append(score)
                                oos_metrics.append(met)
                            agg_score = sum(oos_scores) / max(1, len(oos_scores))
                            scored.append({"params": p, "score": agg_score, "folds": oos_metrics})
                        else:
                            cfg = BacktestConfig(
                                symbols=[sym],
                                start=start,
                                end=end,
                                timeframe="1m",
                                features_table=features_table,
                                strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
                                strategy_params={"extras": {**extras, "symbols": [sym]}},
                                initial_cash=100000.0,
                                commission_per_share=0.0035,
                                commission_min=1.0,
                                slippage_bps=1.0,
                                fill_ref="open",
                            )
                            eng = BacktestEngine(cfg)
                            result = eng.run()
                            met = result.get("metrics", {})
                            try:
                                result.pop("trades", None)
                                result.pop("equity_curve", None)
                                result.pop("final_snapshot", None)
                            except Exception:
                                pass
                            del result
                            _gc.collect()
                            score = float(met.get("total_return", 0.0)) * 100.0
                            score -= max(0.0, (met.get("max_drawdown", 0.0) - 0.03)) * 100.0
                            if met.get("profit_factor") and met.get("profit_factor") < 1.1:
                                score -= 10.0
                            scored.append({"params": p, "score": score, "metrics": met})
                    except Exception as e:
                        scored.append({"params": p, "score": -1e9, "error": str(e)})
            except Exception as e:
                # Fallback to heuristic scoring
                args.real_backtest = False
                print(json.dumps({"warn": "real_backtest_disabled", "reason": str(e)}))

        if not args.real_backtest:
            for i, p in enumerate(candidates):
                score = 0.0
                cf = p.get("market_filter.conf_floor", 0.6)
                sl = p.get("risk.stop_loss_pct", 0.01)
                bq = p.get("sizing.fixed.base_qty", 100)
                score += (cf - 0.5) * 10
                score += (0.012 - abs(sl - 0.012)) * 100
                score += (0.15 - abs((bq/200.0) - 0.5)) * 10
                scored.append({"params": p, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        topk = scored[:5]
        payload = {
            "symbol": sym,
            "day": day,
            "samples": args.samples,
            "top": topk
        }
        Path(out_dir / f"{sym}.json").write_text(json.dumps(payload, indent=2))

    # Optional report
    if args.report_md:
        lines = ["# Nightly HPO Suggestions", ""]
        lines.append(f"Date: {day}")
        lines.append("")
        for sym in symbols:
            j = json.loads((Path(out_dir) / f"{sym}.json").read_text())
            lines.append(f"## {sym}")
            for i, ent in enumerate(j.get("top", [])[:5], 1):
                lines.append(f"{i}. score={ent.get('score')} params={json.dumps(ent.get('params'))}")
            lines.append("")
        Path(args.report_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_md).write_text("\n".join(lines))

    # Collect and aggregate HPO metrics from individual symbol JSONs
    aggregated_hpo_metrics: Dict[str, Any] = {
        "trials": 0,
        "unique_params": 0,
        "best_sharpe": -1e9,
        "best_profit_factor": -1e9,
        "convergence_score": 0.0, # Placeholder, calculate if needed
    }
    all_metrics_from_files: List[Dict[str, Any]] = []

    for sym in symbols:
        symbol_json_path = out_dir / f"{sym}.json"
        if symbol_json_path.exists():
            try:
                data = json.loads(symbol_json_path.read_text("utf-8"))
                
                # Aggregate trials and unique_params
                if "top" in data and isinstance(data["top"], list):
                    all_params_for_sym = []
                    all_scores_for_sym = []
                    for entry in data["top"]:
                        if isinstance(entry, dict) and "params" in entry:
                            all_params_for_sym.append(json.dumps(entry["params"], sort_keys=True))
                            if "score" in entry:
                                all_scores_for_sym.append(float(entry.get("score", 0.0)))
                                
                    aggregated_hpo_metrics["trials"] += len(all_params_for_sym)
                    aggregated_hpo_metrics["unique_params"] += len(set(all_params_for_sym))
                    
                    if all_scores_for_sym:
                        # Find best sharpe/profit_factor if available in individual metrics
                        for entry in data["top"]:
                            if "metrics" in entry and isinstance(entry["metrics"], dict):
                                if "sharpe" in entry["metrics"]:
                                    aggregated_hpo_metrics["best_sharpe"] = max(aggregated_hpo_metrics["best_sharpe"], float(entry["metrics"]["sharpe"]))
                                if "profit_factor" in entry["metrics"]:
                                    aggregated_hpo_metrics["best_profit_factor"] = max(aggregated_hpo_metrics["best_profit_factor"], float(entry["metrics"]["profit_factor"]))
                        
                        # Add scores for convergence calculation
                        all_metrics_from_files.extend(all_scores_for_sym)

            except Exception as e:
                pass # Silently skip malformed JSON files
    
    # Calculate overall convergence score if there are enough samples
    if len(all_metrics_from_files) >= 2:
        import statistics
        top_scores_overall = sorted(all_metrics_from_files, reverse=True)[:5]
        aggregated_hpo_metrics["convergence_score"] = statistics.stdev(top_scores_overall) if len(top_scores_overall) > 1 else 0.0
    else:
        aggregated_hpo_metrics["convergence_score"] = 0.0

    final_payload = {
        "ok": True,
        "symbols": symbols,
        "out_dir": str(out_dir),
        "hpo_metrics": aggregated_hpo_metrics # Add aggregated metrics
    }

    print(json.dumps(final_payload, indent=2))


if __name__ == "__main__":
    main()
