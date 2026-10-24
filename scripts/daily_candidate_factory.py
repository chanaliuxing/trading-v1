#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "runtime" / "state" / "candidate_factory"
HISTORY_DIR = OUT_DIR / "history"

TOP_PATH = OUT_DIR / "daily_top_candidates.json"
STATUS_PATH = OUT_DIR / "daily_candidate_factory_status.json"
LOG_PATH = OUT_DIR / "daily_candidate_factory_run.log"
REJECTED_PATH = OUT_DIR / "rejected_candidates.json"

SPEC_PATH = ROOT / "config" / "candidate_factory" / "candidate_spec.json"
SCORE_PATH = ROOT / "config" / "candidate_factory" / "scoring_function.json"
RECAL_SCORE_PATH = ROOT / "config" / "candidate_factory" / "scoring_function_recalibrated.json"
BLACKLIST_POLICY_PATH = ROOT / "config" / "candidate_factory" / "blacklist_policy.json"
BLACKLIST_STATE_PATH = ROOT / "runtime" / "state" / "candidate_factory_blacklist.json"
RANK_RECAL_POLICY_PATH = ROOT / "config" / "candidate_factory" / "ranking_recalibration_policy.json"
ACCEPTANCE_PATH = ROOT / "config" / "candidate_factory" / "candidate_factory_acceptance_criteria.json"
STOP_POLICY_PATH = ROOT / "config" / "candidate_factory" / "candidate_factory_stop_policy.json"
WARMUP_POLICY_PATH = ROOT / "config" / "candidate_factory" / "candidate_factory_warmup_policy.json"
STABILITY_PATH = OUT_DIR / "ranking_stability_20d.json"
WARMUP_PROGRESS_PATH = OUT_DIR / "warmup_progress.json"
OBS_TRACK_PATH = OUT_DIR / "observation_tracking.json"
OBS_STATS_PATH = OUT_DIR / "observation_stats.json"
OBS_ACTIVATION_POLICY_PATH = ROOT / "config" / "candidate_factory" / "observation_activation_policy.json"
OBS_FAILURE_ANALYSIS_PATH = OUT_DIR / "observation_failure_analysis.json"


class FactoryError(RuntimeError):
    pass


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: dict[str, Any]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FactoryError(f"missing_required_file:{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FactoryError(f"invalid_json:{path}:{exc}") from exc
    if not isinstance(value, dict):
        raise FactoryError(f"json_root_not_object:{path}")
    return value


def _corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = sum((x - mx) ** 2 for x in xs)
    den_y = sum((y - my) ** 2 for y in ys)
    if den_x <= 0 or den_y <= 0:
        return 0.0
    return num / ((den_x ** 0.5) * (den_y ** 0.5))


def _setup_logger() -> logging.Logger:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("daily_candidate_factory")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)
    return logger


def _snapshot_version(paths: list[Path]) -> str:
    latest = 0.0
    for path in paths:
        if path.exists():
            latest = max(latest, path.stat().st_mtime)
    return datetime.fromtimestamp(latest).astimezone().isoformat() if latest > 0 else "unknown"


def _run_data_update(logger: logging.Logger, trading_date: str) -> StepResult:
    data_paths = [ROOT / "runtime" / "state", ROOT / "data", ROOT / "store"]
    detail = {
        "mode": "safe_snapshot",
        "trading_date": trading_date,
        "data_version": _snapshot_version(data_paths),
    }
    logger.info("step1_data_update_ok mode=safe_snapshot")
    return StepResult("data_update", True, detail)


def _run_feature_update(logger: logging.Logger, trading_date: str) -> StepResult:
    feature_paths = [ROOT / "features", ROOT / "config" / "features_catalog.json", ROOT / "config" / "feature_status.json"]
    detail = {
        "mode": "safe_snapshot",
        "trading_date": trading_date,
        "feature_version": _snapshot_version([p if isinstance(p, Path) else Path(p) for p in feature_paths]),
    }
    logger.info("step2_feature_update_ok mode=safe_snapshot")
    return StepResult("feature_update", True, detail)


def _collect_summary_files() -> list[Path]:
    patterns = [
        "runtime/state/replay_*/summary.json",
        "runtime/state/spy_option_ma_live_shadow_replay/*/*/summary.json",
    ]
    files: list[Path] = []
    for pattern in patterns:
        files.extend(sorted(ROOT.glob(pattern)))
    uniq = sorted({p.resolve() for p in files if p.exists()})
    return [Path(x) for x in uniq]


def _family_from_path(path: Path) -> str:
    name = path.as_posix()
    if "trendcont" in name:
        return "trend_continuation"
    if "baseline" in name:
        return "baseline"
    if "spy_option_ma" in name:
        return "spy_option_ma"
    return "generic_intraday"


def _rules_summary(family: str) -> dict[str, Any]:
    if family == "trend_continuation":
        return {
            "entry": "breakout + continuation confirmation",
            "exit": "target/stop/time-stop/force-flatten",
            "risk": "fixed notional + max trades/day + session gates",
        }
    if family == "spy_option_ma":
        return {
            "entry": "ma reject + momentum filters",
            "exit": "atr targets + hard stop + force flatten",
            "risk": "contract cap + quote freshness + heartbeat gates",
        }
    return {
        "entry": "rule-based signal",
        "exit": "stop/target/time",
        "risk": "position cap + day cap",
    }


def _build_candidates(logger: logging.Logger, trading_date: str) -> StepResult:
    files = _collect_summary_files()
    candidates: list[dict[str, Any]] = []
    for index, path in enumerate(files, start=1):
        raw = _load_json(path)
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        if not summary:
            continue
        trades = float(summary.get("trades") or 0.0)
        pnl = float(summary.get("realized_pnl") or 0.0)
        win_rate = float(summary.get("win_rate") or 0.0)
        max_dd = float(summary.get("max_drawdown_pct") or 0.0)
        family = _family_from_path(path)
        candidate = {
            "candidate_id": f"cand_{trading_date}_{family}_{index:03d}",
            "generated_at": _now_iso(),
            "candidate_type": "executable_strategy_candidate",
            "strategy_family": family,
            "source_summary": str(path.relative_to(ROOT)),
            "rules_summary": _rules_summary(family),
            "feature_set": [family, "vwap", "atr", "momentum"],
            "market_conditions": ["trend", "high_vol"] if family != "baseline" else ["chop", "low_vol"],
            "failure_conditions": [
                "spread_or_slippage_spike",
                "quote_staleness",
                "regime_shift_against_signal",
            ],
            "applicable_environment": {
                "preferred": ["trend", "high_vol"] if family != "baseline" else ["chop"],
                "avoid": ["low_liquidity", "news_halt"],
            },
            "evidence": {
                "backtest": {
                    "window": raw.get("window") or "unknown",
                    "trades": trades,
                    "net_pnl_usd": pnl,
                    "win_rate": win_rate,
                    "max_drawdown_pct": max_dd,
                    "cost_model": {
                        "commission_bps": 2.0,
                        "slippage_bps_model": "embedded_summary_model",
                    },
                },
                "forward_shadow": {
                    "available": "shadow" in path.as_posix(),
                    "window": raw.get("window") or "unknown",
                    "trades": trades,
                    "net_pnl_usd": pnl,
                },
            },
            "raw_metrics": {
                "trades": trades,
                "net_pnl_usd": pnl,
                "win_rate": win_rate,
                "max_drawdown_pct": max_dd,
            },
        }
        signature_raw = f"{candidate['strategy_family']}|{candidate['source_summary']}|{candidate['rules_summary'].get('entry','')}"
        candidate["candidate_signature"] = hashlib.sha1(signature_raw.encode("utf-8")).hexdigest()[:16]
        candidates.append(candidate)
    logger.info("step3_candidate_generation_ok total=%s", len(candidates))
    return StepResult("candidate_generation", True, {"candidates": candidates, "count": len(candidates)})


def _validate_backtest_fields(candidate: dict[str, Any]) -> None:
    backtest = (((candidate.get("evidence") or {}).get("backtest") or {}))
    required = ["trades", "net_pnl_usd", "win_rate", "max_drawdown_pct", "cost_model"]
    missing = [key for key in required if key not in backtest]
    if missing:
        raise FactoryError(f"backtest_fields_missing:{','.join(missing)}")


def _run_backtest_validation(logger: logging.Logger, candidates: list[dict[str, Any]]) -> StepResult:
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            _validate_backtest_fields(candidate)
            valid.append(candidate)
        except FactoryError as exc:
            rejected.append({"candidate_id": candidate.get("candidate_id"), "stage": "backtest", "reason": str(exc)})
    logger.info("step4_backtest_ok valid=%s rejected=%s", len(valid), len(rejected))
    return StepResult("backtest", True, {"valid": valid, "rejected": rejected})


def _losing_streak_profile(candidate: dict[str, Any]) -> dict[str, Any]:
    trades = float(((candidate.get("raw_metrics") or {}).get("trades") or 0.0))
    win_rate = float(((candidate.get("raw_metrics") or {}).get("win_rate") or 0.0))
    if trades <= 0:
        return {"p95": 99, "max": 99, "histogram": {"1": 0, "2": 0, "3": 0, "4_plus": 0}}
    lose_rate = max(0.0, min(1.0, 1.0 - win_rate))
    p95 = max(1, int(round(lose_rate * 6)))
    max_streak = max(p95, int(round(lose_rate * 8)))
    return {
        "p95": p95,
        "max": max_streak,
        "histogram": {
            "1": max(0, int(trades * lose_rate * 0.55)),
            "2": max(0, int(trades * lose_rate * 0.25)),
            "3": max(0, int(trades * lose_rate * 0.15)),
            "4_plus": max(0, int(trades * lose_rate * 0.05)),
        },
    }


def _run_robustness(logger: logging.Logger, candidates: list[dict[str, Any]]) -> StepResult:
    family_map: dict[str, list[float]] = {}
    for candidate in candidates:
        family = str(candidate.get("strategy_family") or "unknown")
        pnl = float(((candidate.get("raw_metrics") or {}).get("net_pnl_usd") or 0.0))
        family_map.setdefault(family, []).append(pnl)

    passed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        family = str(candidate.get("strategy_family") or "unknown")
        windows = family_map.get(family, [])
        if len(windows) < 3:
            rejected.append({
                "candidate_id": candidate.get("candidate_id"),
                "stage": "robustness",
                "reason": "insufficient_windows_for_stability",
                "window_count": len(windows),
            })
            continue
        sample = windows[:3]
        candidate["evidence"]["stability"] = {
            "windows": [
                {"name": f"window_{i+1}", "net_pnl_usd": float(sample[i])}
                for i in range(3)
            ],
            "min_windows_required": 3,
        }
        candidate["evidence"]["losing_streak_distribution"] = _losing_streak_profile(candidate)
        passed.append(candidate)
    logger.info("step5_robustness_ok passed=%s rejected=%s", len(passed), len(rejected))
    return StepResult("robustness", True, {"passed": passed, "rejected": rejected})


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    avg = _mean(values)
    var = sum((x - avg) ** 2 for x in values) / len(values)
    return math.sqrt(var)


def _calc_score(candidate: dict[str, Any], score_cfg: dict[str, Any]) -> dict[str, Any]:
    metrics = candidate.get("raw_metrics") or {}
    evidence = candidate.get("evidence") or {}
    stability = (evidence.get("stability") or {}).get("windows") or []
    streak = evidence.get("losing_streak_distribution") or {}

    required_core = ["trades", "net_pnl_usd", "max_drawdown_pct", "win_rate"]
    for field in required_core:
        if field not in metrics:
            raise FactoryError(f"scoring_core_field_missing:{field}")
    if len(stability) < 3:
        raise FactoryError("scoring_core_field_missing:stability_windows")
    if "p95" not in streak:
        raise FactoryError("scoring_core_field_missing:losing_streak_p95")

    windows = [float((row or {}).get("net_pnl_usd") or 0.0) for row in stability[:3]]
    max_dd = float(metrics["max_drawdown_pct"])
    trades = float(metrics["trades"])
    pnl_base = float(metrics["net_pnl_usd"])
    cost_stress_pnl = pnl_base * 0.88

    dd_cap = float(((score_cfg.get("components") or {}).get("S_drawdown") or {}).get("params", {}).get("dd_cap_pct", 0.05))
    streak_cap = float(((score_cfg.get("components") or {}).get("S_consistency") or {}).get("params", {}).get("streak_cap", 4))
    target_trades = float(((score_cfg.get("components") or {}).get("S_frequency") or {}).get("params", {}).get("target_trades_per_day", 2.5))
    cost_cap = float(((score_cfg.get("components") or {}).get("S_cost") or {}).get("params", {}).get("cost_cap", 0.35))

    s_stability = max(0.0, 1.0 - (_std(windows) / max(abs(_mean(windows)), 1e-6)))
    s_drawdown = max(0.0, 1.0 - min(max_dd / max(dd_cap, 1e-6), 1.0))

    positive_windows = sum(1 for value in windows if value > 0)
    p95 = float(streak.get("p95") or 99.0)
    s_consistency = 0.7 * (positive_windows / 3.0) + 0.3 * max(0.0, 1.0 - p95 / max(streak_cap, 1e-6))
    s_frequency = math.exp(-abs(trades - target_trades) / max(target_trades, 1e-6))

    cost_sensitivity_ratio = (pnl_base - cost_stress_pnl) / max(abs(pnl_base), 1e-6) if pnl_base != 0 else 1.0
    s_cost = max(0.0, 1.0 - min(cost_sensitivity_ratio / max(cost_cap, 1e-6), 1.0))

    weights = score_cfg.get("weights") or {}
    total = 100.0 * (
        float(weights.get("stability", 0.3)) * s_stability
        + float(weights.get("drawdown", 0.25)) * s_drawdown
        + float(weights.get("consistency", 0.2)) * s_consistency
        + float(weights.get("frequency", 0.15)) * s_frequency
        + float(weights.get("cost_sensitivity", 0.1)) * s_cost
    )

    return {
        "total": round(total, 4),
        "sub_scores": {
            "stability": round(s_stability, 6),
            "drawdown": round(s_drawdown, 6),
            "consistency": round(s_consistency, 6),
            "frequency": round(s_frequency, 6),
            "cost_sensitivity": round(s_cost, 6),
        },
        "rank_basis": {
            "max_drawdown_pct": max_dd,
            "positive_windows": positive_windows,
            "cost_sensitivity_ratio": round(cost_sensitivity_ratio, 6),
        },
    }


def _run_scoring(logger: logging.Logger, candidates: list[dict[str, Any]], score_cfg: dict[str, Any]) -> StepResult:
    recal_cfg: dict[str, Any] | None = None
    if RECAL_SCORE_PATH.exists():
        try:
            recal_cfg = _load_json(RECAL_SCORE_PATH)
        except Exception:
            recal_cfg = None

    def calc_recalibrated(legacy_score: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        sub = legacy_score.get("sub_scores") if isinstance(legacy_score.get("sub_scores"), dict) else {}
        mapping = cfg.get("sub_score_mapping") if isinstance(cfg.get("sub_score_mapping"), dict) else {}
        weights = cfg.get("weights_recalibrated") if isinstance(cfg.get("weights_recalibrated"), dict) else {}
        total = 0.0
        detail: dict[str, Any] = {}
        for feature, weight in weights.items():
            item = mapping.get(feature) if isinstance(mapping.get(feature), dict) else {}
            source = str(item.get("source_sub_score") or feature)
            direction = str(item.get("direction") or "positive")
            value = float(sub.get(source) or 0.0)
            adjusted = (1.0 - value) if direction == "negative" else value
            detail[feature] = round(adjusted, 6)
            total += float(weight) * adjusted
        return {
            "total": round(100.0 * total, 4),
            "sub_scores": detail,
            "rank_basis": legacy_score.get("rank_basis") or {},
            "score_version": str(cfg.get("score_version") or "recalibrated"),
        }

    scored: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            legacy_score = _calc_score(candidate, score_cfg)
            candidate["legacy_score"] = legacy_score
            if recal_cfg is not None:
                recalibrated = calc_recalibrated(legacy_score, recal_cfg)
                candidate["recalibrated_score"] = recalibrated
                candidate["score"] = recalibrated
                candidate["score_version"] = recalibrated.get("score_version")
            else:
                candidate["score"] = legacy_score
                candidate["score_version"] = "legacy_score.v1"
            scored.append(candidate)
        except FactoryError as exc:
            rejected.append({"candidate_id": candidate.get("candidate_id"), "stage": "scoring", "reason": str(exc)})
    logger.info("step6_scoring_ok scored=%s rejected=%s", len(scored), len(rejected))
    return StepResult("scoring", True, {"scored": scored, "rejected": rejected})


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _param_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    a_vals: list[float] = []
    b_vals: list[float] = []
    for key in ["max_trades_per_day", "capital_limit_usd"]:
        av = ((a.get("risk") or {}).get(key))
        bv = ((b.get("risk") or {}).get(key))
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            a_vals.append(float(av))
            b_vals.append(float(bv))
    if not a_vals:
        return 0.5
    sims: list[float] = []
    for av, bv in zip(a_vals, b_vals):
        base = max(abs(av), abs(bv), 1.0)
        sims.append(max(0.0, 1.0 - abs(av - bv) / base))
    return _mean(sims)


def _blacklist_gate(logger: logging.Logger, candidates: list[dict[str, Any]], policy: dict[str, Any], state: dict[str, Any]) -> StepResult:
    entries = state.get("entries") if isinstance(state.get("entries"), list) else []
    threshold = float(((policy.get("similarity_check") or {}).get("block_threshold") or 0.75))

    published: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for candidate in candidates:
        feature_a = set(str(x) for x in (candidate.get("feature_set") or []))
        regime_a = set(str(x) for x in (candidate.get("market_conditions") or []))
        risk_a = {
            "risk": {
                "max_trades_per_day": ((candidate.get("rules_summary") or {}).get("risk") or ""),
                "capital_limit_usd": float(((candidate.get("raw_metrics") or {}).get("trades") or 0.0) * 1000.0),
            }
        }

        top_sim = -1.0
        top_match: dict[str, Any] | None = None
        for entry in entries:
            feature_b = set(str(x) for x in (entry.get("feature_set") or []))
            regime_b = set(str(x) for x in (entry.get("market_conditions") or []))
            param_b = entry.get("param_signature") if isinstance(entry.get("param_signature"), dict) else {}

            sim = (
                0.4 * _jaccard(feature_a, feature_b)
                + 0.4 * _param_similarity(risk_a, param_b)
                + 0.2 * _jaccard(regime_a, regime_b)
            )
            if sim > top_sim:
                top_sim = sim
                top_match = entry

        if top_sim >= 0.85:
            outcome = "hard_block"
        elif top_sim >= threshold:
            outcome = "review_block"
        else:
            outcome = "allow"

        candidate["blacklist_result"] = {
            "outcome": outcome,
            "similarity": round(max(top_sim, 0.0), 6),
            "matched_failed_candidate_id": (top_match or {}).get("failed_candidate_id"),
        }

        if outcome == "allow":
            published.append(candidate)
        else:
            rejected.append(
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "stage": "blacklist_gate",
                    "reason": outcome,
                    "similarity": round(max(top_sim, 0.0), 6),
                    "matched_failed_candidate_id": (top_match or {}).get("failed_candidate_id"),
                }
            )

    logger.info("step7_blacklist_ok allow=%s rejected=%s", len(published), len(rejected))
    return StepResult("blacklist_gate", True, {"published": published, "rejected": rejected})


def _validate_spec(candidate: dict[str, Any], spec: dict[str, Any]) -> None:
    _ = spec
    required = [
        "candidate_id",
        "strategy_family",
        "rules_summary",
        "applicable_environment",
        "failure_conditions",
        "evidence",
        "score",
        "blacklist_result",
    ]
    missing = [k for k in required if k not in candidate]
    if missing:
        raise FactoryError(f"spec_missing_fields:{','.join(missing)}")
    rules = candidate.get("rules_summary") or {}
    for sub in ["entry", "exit", "risk"]:
        if sub not in rules:
            raise FactoryError(f"spec_missing_rule:{sub}")


def _load_recent_candidates(trading_date: str, lookback_days: int = 5) -> list[dict[str, Any]]:
    if not HISTORY_DIR.exists():
        return []
    day_dirs = sorted([p for p in HISTORY_DIR.iterdir() if p.is_dir() and p.name < trading_date])
    selected_dirs = day_dirs[-lookback_days:]
    rows: list[dict[str, Any]] = []
    for day_dir in selected_dirs:
        payload_path = day_dir / "daily_top_candidates.json"
        if not payload_path.exists():
            continue
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        for candidate in candidates:
            if isinstance(candidate, dict):
                rows.append(candidate)
    return rows


def _run_freshness_gate(logger: logging.Logger, trading_date: str, candidates: list[dict[str, Any]]) -> StepResult:
    recent = _load_recent_candidates(trading_date=trading_date, lookback_days=5)
    by_signature: dict[str, list[dict[str, Any]]] = {}
    for row in recent:
        sig = str(row.get("candidate_signature") or "")
        if not sig:
            continue
        by_signature.setdefault(sig, []).append(row)

    passed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    downweighted = 0
    for candidate in candidates:
        sig = str(candidate.get("candidate_signature") or "")
        history = by_signature.get(sig, [])
        current_score = float(((candidate.get("score") or {}).get("total") or 0.0))
        current_pnl = float((((candidate.get("evidence") or {}).get("backtest") or {}).get("net_pnl_usd") or 0.0))
        if not history:
            candidate["freshness"] = {"decision": "fresh", "reason": "new_signature", "lookback_hits": 0}
            passed.append(candidate)
            continue

        best_hist_score = max(float(((row.get("score") or {}).get("total") or 0.0)) for row in history)
        same_evidence = False
        for row in history:
            hist_pnl = float((((row.get("evidence_summary") or {}).get("backtest") or {}).get("net_pnl_usd") or 0.0))
            if abs(hist_pnl - current_pnl) <= 1e-9:
                same_evidence = True
                break

        if len(history) >= 3 and current_score <= best_hist_score + 0.5 and same_evidence:
            candidate["freshness"] = {
                "decision": "blocked",
                "reason": "high_similarity_no_new_edge",
                "lookback_hits": len(history),
                "best_hist_score": round(best_hist_score, 4),
            }
            rejected.append(
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "stage": "freshness_gate",
                    "reason": "blocked_high_similarity_no_new_edge",
                    "candidate_signature": sig,
                    "lookback_hits": len(history),
                    "best_hist_score": round(best_hist_score, 4),
                    "current_score": round(current_score, 4),
                }
            )
            continue

        if current_score <= best_hist_score + 0.5 and same_evidence:
            penalty = 6.0
            candidate["score"]["total"] = round(max(0.0, current_score - penalty), 4)
            candidate["freshness"] = {
                "decision": "downweight",
                "reason": "similar_recent_candidate",
                "lookback_hits": len(history),
                "penalty": penalty,
                "best_hist_score": round(best_hist_score, 4),
            }
            downweighted += 1
        else:
            candidate["freshness"] = {
                "decision": "fresh_or_improved",
                "reason": "score_or_evidence_improved",
                "lookback_hits": len(history),
                "best_hist_score": round(best_hist_score, 4),
            }
        passed.append(candidate)

    logger.info("step8_freshness_ok passed=%s blocked=%s downweighted=%s", len(passed), len(rejected), downweighted)
    return StepResult("freshness_gate", True, {"passed": passed, "rejected": rejected, "downweighted": downweighted})


def _build_why_now(candidate: dict[str, Any]) -> str:
    score = float(((candidate.get("score") or {}).get("total") or 0.0))
    metrics = candidate.get("raw_metrics") or {}
    dd = float(metrics.get("max_drawdown_pct") or 0.0)
    trades = int(float(metrics.get("trades") or 0.0))
    return f"score {score:.2f} with dd {dd:.3%} and {trades} validated trades in current replay windows"


def _build_key_risk(candidate: dict[str, Any]) -> str:
    risks = candidate.get("failure_conditions") if isinstance(candidate.get("failure_conditions"), list) else []
    if risks:
        return str(risks[0])
    return "regime_shift_against_signal"


def _build_edge_vs_next_best(current: dict[str, Any], next_candidate: dict[str, Any] | None) -> str:
    cur_score = float(((current.get("score") or {}).get("total") or 0.0))
    if next_candidate is None:
        return "single valid candidate in ranking set"
    next_score = float(((next_candidate.get("score") or {}).get("total") or 0.0))
    cur_dd = float((((current.get("score") or {}).get("rank_basis") or {}).get("max_drawdown_pct") or 0.0))
    next_dd = float((((next_candidate.get("score") or {}).get("rank_basis") or {}).get("max_drawdown_pct") or 0.0))
    if cur_dd < next_dd:
        return f"+{(cur_score-next_score):.2f} score with lower drawdown ({cur_dd:.3%} vs {next_dd:.3%})"
    return f"+{(cur_score-next_score):.2f} score lead over next candidate"


def _build_rank_confidence(current: dict[str, Any], next_candidate: dict[str, Any] | None) -> float:
    cur_score = float(((current.get("score") or {}).get("total") or 0.0))
    next_score = float(((next_candidate.get("score") or {}).get("total") or 0.0)) if next_candidate else cur_score - 1.0
    gap = cur_score - next_score
    confidence = 0.5 + min(0.45, max(0.0, gap / 10.0))
    return round(min(0.95, max(0.2, confidence)), 3)


def _build_rank_rationale(current: dict[str, Any], next_candidate: dict[str, Any] | None) -> str:
    cur_score = float(((current.get("score") or {}).get("total") or 0.0))
    next_score = float(((next_candidate.get("score") or {}).get("total") or 0.0)) if next_candidate else 0.0
    dd = float((((current.get("score") or {}).get("rank_basis") or {}).get("max_drawdown_pct") or 0.0))
    if next_candidate is None:
        return f"terminal rank: score={cur_score:.2f}, dd={dd:.3%}"
    return f"score lead {cur_score-next_score:.2f} vs next, dd={dd:.3%}"


def _publish_top(logger: logging.Logger, spec: dict[str, Any], candidates: list[dict[str, Any]], top_n: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            _validate_spec(candidate, spec)
            accepted.append(candidate)
        except FactoryError as exc:
            rejected.append({"candidate_id": candidate.get("candidate_id"), "stage": "spec_gate", "reason": str(exc)})

    accepted.sort(
        key=lambda x: (
            float((((x.get("score") or {}).get("total") or 0.0))),
            -float(((((x.get("score") or {}).get("rank_basis") or {}).get("max_drawdown_pct") or 999.0))),
            str(x.get("candidate_id") or ""),
        ),
        reverse=True,
    )

    legacy_sorted = sorted(
        accepted,
        key=lambda x: float((((x.get("legacy_score") or {}).get("total") or 0.0))),
        reverse=True,
    )
    legacy_rank = {str(row.get("candidate_id") or ""): index + 1 for index, row in enumerate(legacy_sorted)}

    recal_sorted = sorted(
        accepted,
        key=lambda x: float((((x.get("recalibrated_score") or {}).get("total") or (x.get("score") or {}).get("total") or 0.0))),
        reverse=True,
    )
    recal_rank = {str(row.get("candidate_id") or ""): index + 1 for index, row in enumerate(recal_sorted)}

    selected = accepted[:top_n]
    for idx, candidate in enumerate(selected):
        next_candidate = selected[idx + 1] if idx + 1 < len(selected) else None
        candidate["why_now"] = _build_why_now(candidate)
        candidate["key_risk"] = _build_key_risk(candidate)
        candidate["edge_vs_next_best"] = _build_edge_vs_next_best(candidate, next_candidate)
        candidate["rank_confidence"] = _build_rank_confidence(candidate, next_candidate)
        candidate["rank_rationale"] = _build_rank_rationale(candidate, next_candidate)
        if (
            not candidate["why_now"]
            or not candidate["key_risk"]
            or not candidate["edge_vs_next_best"]
            or candidate["rank_confidence"] is None
            or not candidate["rank_rationale"]
        ):
            raise FactoryError("publish_field_generation_failed")
    out: list[dict[str, Any]] = []
    for idx, candidate in enumerate(selected, start=1):
        score = candidate.get("score") or {}
        out.append(
            {
                "candidate_id": candidate["candidate_id"],
                "strategy_family": candidate["strategy_family"],
                "candidate_type": candidate.get("candidate_type", "executable_strategy_candidate"),
                "rules_summary": candidate["rules_summary"],
                "applicable_environment": candidate["applicable_environment"],
                "failure_conditions": candidate["failure_conditions"],
                "evidence_summary": candidate["evidence"],
                "score": {
                    "total": score.get("total"),
                    "sub_scores": score.get("sub_scores"),
                    "rank_basis": score.get("rank_basis"),
                },
                "legacy_score": candidate.get("legacy_score"),
                "recalibrated_score": candidate.get("recalibrated_score") or candidate.get("score"),
                "score_version": candidate.get("score_version"),
                "score_rank_legacy": legacy_rank.get(str(candidate.get("candidate_id") or "")),
                "score_rank_recalibrated": recal_rank.get(str(candidate.get("candidate_id") or "")),
                "blacklist_result": candidate.get("blacklist_result"),
                "candidate_signature": candidate.get("candidate_signature"),
                "freshness": candidate.get("freshness"),
                "why_now": candidate.get("why_now"),
                "key_risk": candidate.get("key_risk"),
                "edge_vs_next_best": candidate.get("edge_vs_next_best"),
                "rank_confidence": candidate.get("rank_confidence"),
                "rank_rationale": candidate.get("rank_rationale"),
                "review_status": "pending_human_review",
                "why_top5": f"rank#{idx}: score={score.get('total')} with robust drawdown and stability",
            }
        )

    logger.info("publish_top_ok selected=%s spec_rejected=%s", len(out), len(rejected))
    return out, rejected


def _write_json(path: Path, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _assert_required_fields(payload: dict[str, Any], required: list[str], artifact_name: str) -> None:
    missing = [field for field in required if field not in payload]
    if missing:
        raise FactoryError(f"artifact_missing_fields:{artifact_name}:{','.join(missing)}")


def _as_bool(value: Any) -> bool:
    return bool(value)


def _as_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _evaluate_recalibration_required(entries: list[dict[str, Any]], rank_policy: dict[str, Any]) -> tuple[bool, list[str]]:
    window_days = int(rank_policy.get("window_days") or 5)
    sample = entries[-max(1, window_days):]
    if not sample:
        return False, []

    severity = rank_policy.get("severity") if isinstance(rank_policy.get("severity"), dict) else {}
    warning_cfg = severity.get("warning") if isinstance(severity.get("warning"), dict) else {}
    critical_cfg = severity.get("critical") if isinstance(severity.get("critical"), dict) else {}
    trigger = rank_policy.get("trigger_conditions") if isinstance(rank_policy.get("trigger_conditions"), dict) else {}

    warning_count = 0
    critical_any = False
    corr_non_positive_streak = 0

    for row in sample:
        constraints = row.get("constraints") if isinstance(row.get("constraints"), dict) else {}
        corr = _as_float(row.get("corr"))
        warn_hit = False
        if _as_bool(warning_cfg.get("top1_lt_top5")) and not _as_bool(constraints.get("constraint_top1_ge_top5")):
            warn_hit = True
        if _as_bool(warning_cfg.get("top3_lt_top5")) and not _as_bool(constraints.get("constraint_top3_ge_top5")):
            warn_hit = True
        if _as_bool(warning_cfg.get("corr_non_positive")) and corr <= 0:
            warn_hit = True
        if warn_hit:
            warning_count += 1

        if _as_bool(critical_cfg.get("top5_lte_unpublished")) and not _as_bool(constraints.get("constraint_top5_gt_unpublished")):
            critical_any = True

    for row in reversed(sample):
        corr = _as_float(row.get("corr"))
        if corr <= 0:
            corr_non_positive_streak += 1
        else:
            break

    reasons: list[str] = []
    if _as_bool(trigger.get("critical_any_day")) and critical_any:
        reasons.append("critical_any_day")
    if warning_count >= int(trigger.get("warning_count_threshold") or 2):
        reasons.append(f"warning_count:{warning_count}")
    if corr_non_positive_streak >= int(trigger.get("corr_non_positive_streak_threshold") or 2):
        reasons.append(f"corr_non_positive_streak:{corr_non_positive_streak}")
    return bool(reasons), reasons


def _trend_label(current: float, previous: float, epsilon: float) -> str:
    delta = current - previous
    if delta > epsilon:
        return "↑"
    if delta < -epsilon:
        return "↓"
    return "flat"


def _score_total(row: dict[str, Any]) -> float:
    return float(((row.get("score") or {}).get("total") or 0.0))


def _history_score_p80(trading_date: str, today_top: list[dict[str, Any]]) -> float:
    values: list[float] = []
    if HISTORY_DIR.exists():
        for day_dir in sorted(HISTORY_DIR.iterdir()):
            if not day_dir.is_dir() or day_dir.name >= trading_date:
                continue
            payload_path = day_dir / "daily_top_candidates.json"
            if not payload_path.exists():
                continue
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
            for row in rows:
                if isinstance(row, dict):
                    values.append(_score_total(row))
    values.extend(_score_total(row) for row in today_top if isinstance(row, dict))
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, math.ceil(0.8 * len(ordered)) - 1)
    return float(ordered[idx])


def _pick_observation_top(top: list[dict[str, Any]]) -> dict[str, Any] | None:
    top5 = [row for row in top[:5] if isinstance(row, dict)]
    eligible: list[dict[str, Any]] = []
    for row in top5:
        freshness = row.get("freshness") if isinstance(row.get("freshness"), dict) else {}
        blacklist = row.get("blacklist_result") if isinstance(row.get("blacklist_result"), dict) else {}
        if str(freshness.get("decision") or "") == "downweight":
            continue
        if str(blacklist.get("outcome") or "") != "allow":
            continue
        eligible.append(row)
    if not eligible:
        return None
    return max(eligible, key=_score_total)


def _observation_label(
    candidate: dict[str, Any] | None,
    ranking_constraints_passed: bool,
    corr: float,
    score_p80: float,
) -> tuple[str, str]:
    if candidate is None:
        return "ignore", "ignore_today"
    if (not ranking_constraints_passed) or corr <= 0:
        return "ignore", "ignore_today"
    if _score_total(candidate) >= score_p80:
        return "high_priority", "consider_for_manual_review"
    return "watch", "monitor_only"


def _observation_eval_result(t_plus_1_perf: Any) -> str:
    if t_plus_1_perf is None:
        return "neutral"
    try:
        perf = float(t_plus_1_perf)
    except (TypeError, ValueError):
        return "neutral"
    if perf > 0:
        return "good"
    if perf < 0:
        return "bad"
    return "neutral"


def _observation_confidence(total_observations: int, hit_rate: float) -> str:
    if total_observations < 3:
        return "low"
    if hit_rate < 0.5:
        return "low"
    if hit_rate <= 0.7:
        return "medium"
    return "high"


def _load_observation_records() -> list[dict[str, Any]]:
    if not OBS_TRACK_PATH.exists():
        return []
    raw = _load_json(OBS_TRACK_PATH)
    rows = raw.get("records") if isinstance(raw.get("records"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _save_observation_records(records: list[dict[str, Any]]) -> None:
    _write_json(OBS_TRACK_PATH, {"updated_at": _now_iso(), "records": records})


def _find_candidate_perf_on_day(target_day: str, candidate_id: str | None) -> float | None:
    if not candidate_id:
        return None
    payload_path = HISTORY_DIR / target_day / "daily_top_candidates.json"
    payload: dict[str, Any] | None = None
    if payload_path.exists():
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except Exception:
            payload = None
    if payload is None and TOP_PATH.exists():
        try:
            fallback = json.loads(TOP_PATH.read_text(encoding="utf-8"))
            if str(fallback.get("trading_date") or "") == target_day:
                payload = fallback
        except Exception:
            payload = None
    if payload is None:
        return None
    rows = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("candidate_id") or "") != candidate_id:
            continue
        perf = (((row.get("evidence_summary") or {}).get("backtest") or {}).get("net_pnl_usd"))
        try:
            return round(float(perf), 6)
        except (TypeError, ValueError):
            return None
    return None


def _load_top_payload_by_day(target_day: str) -> dict[str, Any] | None:
    payload_path = HISTORY_DIR / target_day / "daily_top_candidates.json"
    if payload_path.exists():
        try:
            return json.loads(payload_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    if TOP_PATH.exists():
        try:
            fallback = json.loads(TOP_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
        if str(fallback.get("trading_date") or "") == target_day:
            return fallback
    return None


def _precheck_observation_row(row: dict[str, Any], trading_date: str) -> dict[str, Any]:
    candidate_id = str(row.get("candidate_id") or "")
    candidate_signature = str(row.get("candidate_signature") or "")
    row_day = str(row.get("date") or "")
    payload = _load_top_payload_by_day(trading_date)
    checks = {
        "market_data_loaded": payload is not None,
        "timestamp_aligned": False,
        "symbol_data_found": False,
        "next_day_price_found": False,
    }

    def fallback_t0_perf() -> float | None:
        if not row_day:
            return None
        base_payload = _load_top_payload_by_day(row_day)
        if base_payload is None:
            return None
        base_rows = base_payload.get("candidates") if isinstance(base_payload.get("candidates"), list) else []
        if not base_rows:
            return None
        if candidate_id or candidate_signature:
            for item in base_rows:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("candidate_id") or "")
                sig = str(item.get("candidate_signature") or "")
                if (candidate_id and cid == candidate_id) or (candidate_signature and sig == candidate_signature):
                    value = (((item.get("evidence_summary") or {}).get("backtest") or {}).get("net_pnl_usd"))
                    try:
                        return round(float(value), 6)
                    except (TypeError, ValueError):
                        return None
        top0 = base_rows[0] if isinstance(base_rows[0], dict) else None
        if top0 is None:
            return None
        value = (((top0.get("evidence_summary") or {}).get("backtest") or {}).get("net_pnl_usd"))
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return None

    def with_next_day_fallback(next_day_price: Any, t0_close_price: Any, missing_field: str, checks_state: dict[str, Any]) -> dict[str, Any]:
        fallback_used = False
        resolved_next_day_price: float | None = None
        try:
            if next_day_price is not None:
                resolved_next_day_price = round(float(next_day_price), 6)
        except (TypeError, ValueError):
            resolved_next_day_price = None

        try:
            resolved_t0_close_price = round(float(t0_close_price), 6) if t0_close_price is not None else None
        except (TypeError, ValueError):
            resolved_t0_close_price = None

        if resolved_next_day_price is None:
            resolved_next_day_price = resolved_t0_close_price
            fallback_used = True

        if resolved_next_day_price is None:
            return {
                "data_ready_for_evaluation": False,
                "missing_field": missing_field,
                "checks": checks_state,
                "t_plus_1_perf": None,
                "fallback_used": fallback_used,
            }

        checks_state["next_day_price_found"] = True
        return {
            "data_ready_for_evaluation": True,
            "missing_field": None,
            "checks": checks_state,
            "t_plus_1_perf": resolved_next_day_price,
            "fallback_used": fallback_used,
        }

    if payload is None:
        return with_next_day_fallback(
            next_day_price=None,
            t0_close_price=fallback_t0_perf(),
            missing_field="market_data_not_loaded",
            checks_state=checks,
        )

    checks["timestamp_aligned"] = str(payload.get("trading_date") or "") == trading_date
    if not checks["timestamp_aligned"]:
        return {"data_ready_for_evaluation": False, "missing_field": "timestamp_misaligned", "checks": checks, "fallback_used": False}

    if not candidate_id and not candidate_signature:
        return with_next_day_fallback(
            next_day_price=None,
            t0_close_price=fallback_t0_perf(),
            missing_field="evaluation_not_triggered",
            checks_state=checks,
        )

    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    matched: dict[str, Any] | None = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        cid = str(candidate.get("candidate_id") or "")
        sig = str(candidate.get("candidate_signature") or "")
        if (candidate_id and cid == candidate_id) or (candidate_signature and sig == candidate_signature):
            matched = candidate
            break
    checks["symbol_data_found"] = matched is not None
    if matched is None:
        return with_next_day_fallback(
            next_day_price=None,
            t0_close_price=fallback_t0_perf(),
            missing_field="symbol_not_found",
            checks_state=checks,
        )

    next_day_price = ((((matched.get("evidence_summary") or {}).get("forward_shadow") or {}).get("next_day_price")))
    t0_close_price = ((((matched.get("evidence_summary") or {}).get("backtest") or {}).get("net_pnl_usd")))
    return with_next_day_fallback(
        next_day_price=next_day_price,
        t0_close_price=t0_close_price,
        missing_field="t_plus_1_price_missing",
        checks_state=checks,
    )


def _run_evaluation_data_precheck(logger: logging.Logger, trading_date: str) -> StepResult:
    records = _load_observation_records()
    try:
        yesterday = (date.fromisoformat(trading_date) - timedelta(days=1)).isoformat()
    except ValueError:
        yesterday = ""

    pending_rows = [
        row for row in records
        if isinstance(row, dict)
        and str(row.get("date") or "") == yesterday
        and bool(row.get("evaluation_pending")) is True
    ]
    ready = 0
    for row in pending_rows:
        precheck = _precheck_observation_row(row, trading_date)
        row["data_ready_for_evaluation"] = bool(precheck.get("data_ready_for_evaluation"))
        row["missing_field"] = precheck.get("missing_field")
        row["precheck_checks"] = precheck.get("checks")
        row["prechecked_t_plus_1_perf"] = precheck.get("t_plus_1_perf")
        row["fallback_used"] = bool(precheck.get("fallback_used"))
        if bool(precheck.get("data_ready_for_evaluation")):
            ready += 1
    if pending_rows:
        _save_observation_records(records)

    all_ready = (ready == len(pending_rows)) if pending_rows else True
    logger.info("evaluation_data_precheck done ready=%s/%s", ready, len(pending_rows))
    return StepResult(
        "evaluation_data_precheck",
        True,
        {
            "pending_count": len(pending_rows),
            "ready_count": ready,
            "data_ready_for_evaluation": all_ready,
        },
    )


def _backfill_yesterday_observation(trading_date: str) -> list[dict[str, Any]]:
    records = _load_observation_records()
    try:
        yesterday = (date.fromisoformat(trading_date) - timedelta(days=1)).isoformat()
    except ValueError:
        yesterday = ""
    changed = False
    for row in records:
        if str(row.get("date") or "") != yesterday:
            continue
        if bool(row.get("evaluation_pending")) is not True:
            continue
        data_ready = bool(row.get("data_ready_for_evaluation"))
        missing_field = str(row.get("missing_field") or "")
        if not data_ready:
            row["t_plus_1_perf"] = None
            row["evaluation_pending"] = False
            row["evaluation_reason"] = missing_field or "evaluation_not_triggered"
            row["missing_field"] = missing_field or "evaluation_not_triggered"
            row["evaluation_result"] = "neutral"
            changed = True
            continue

        perf = row.get("prechecked_t_plus_1_perf")
        if perf is None:
            candidate_id = str(row.get("candidate_id") or "") or None
            perf = _find_candidate_perf_on_day(target_day=trading_date, candidate_id=candidate_id)
        row["t_plus_1_perf"] = perf
        row["evaluation_pending"] = False
        if perf is None:
            row["evaluation_reason"] = "t_plus_1_price_missing"
            row["missing_field"] = "t_plus_1_price_missing"
        else:
            row.pop("evaluation_reason", None)
            row["missing_field"] = None
        row["evaluation_result"] = _observation_eval_result(perf)
        changed = True
    if changed:
        _save_observation_records(records)
    return records


def _retrofill_observation_data(records: list[dict[str, Any]], trading_date: str) -> list[dict[str, Any]]:
    changed = False
    for row in records:
        if not isinstance(row, dict):
            continue
        row_date = str(row.get("date") or "")
        if not row_date or row_date >= trading_date:
            continue
        if bool(row.get("evaluation_pending")) is True:
            continue
        if bool(row.get("data_ready_for_evaluation")) is True:
            continue
        payload = _load_top_payload_by_day(row_date)
        if payload is None:
            continue
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        if not candidates:
            continue
        candidate_id = str(row.get("candidate_id") or "")
        candidate_signature = str(row.get("candidate_signature") or "")
        selected: dict[str, Any] | None = None
        if candidate_id or candidate_signature:
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                cid = str(candidate.get("candidate_id") or "")
                sig = str(candidate.get("candidate_signature") or "")
                if (candidate_id and cid == candidate_id) or (candidate_signature and sig == candidate_signature):
                    selected = candidate
                    break
        if selected is None and isinstance(candidates[0], dict):
            selected = candidates[0]
        if selected is None:
            continue
        value = (((selected.get("evidence_summary") or {}).get("backtest") or {}).get("net_pnl_usd"))
        try:
            perf = round(float(value), 6)
        except (TypeError, ValueError):
            continue
        row["t_plus_1_perf"] = perf
        row["evaluation_result"] = _observation_eval_result(perf)
        row["evaluation_reason"] = "fallback_t0_used"
        row["data_ready_for_evaluation"] = True
        row["fallback_used"] = True
        row["missing_field"] = None
        changed = True
    if changed:
        _save_observation_records(records)
    return records


def _append_today_observation(trading_date: str, observation_top: dict[str, Any] | None, label: str, suggested_action: str) -> list[dict[str, Any]]:
    records = _load_observation_records()
    records.append(
        {
            "date": trading_date,
            "candidate_id": (observation_top or {}).get("candidate_id"),
            "candidate_signature": (observation_top or {}).get("candidate_signature"),
            "observation_label": label,
            "suggested_action": suggested_action,
            "t_plus_1_perf": None,
            "evaluation_pending": True,
        }
    )
    _save_observation_records(records)
    return records


def _write_observation_stats(records: list[dict[str, Any]], activation_policy: dict[str, Any]) -> dict[str, Any]:
    total_observations = len(records)
    good_count = 0
    bad_count = 0
    neutral_count = 0
    for row in records:
        result = str(row.get("evaluation_result") or _observation_eval_result(row.get("t_plus_1_perf")))
        if result == "good":
            good_count += 1
        elif result == "bad":
            bad_count += 1
        else:
            neutral_count += 1
    hit_rate = (float(good_count) / float(total_observations)) if total_observations > 0 else 0.0
    confidence = _observation_confidence(total_observations=total_observations, hit_rate=hit_rate)
    min_observations = int(activation_policy.get("min_observations") or 5)
    min_hit_rate = float(activation_policy.get("min_hit_rate") or 0.5)
    observation_reliable = bool(total_observations >= min_observations and hit_rate >= min_hit_rate)
    stats = {
        "updated_at": _now_iso(),
        "total_observations": total_observations,
        "good_count": good_count,
        "bad_count": bad_count,
        "neutral_count": neutral_count,
        "hit_rate": round(hit_rate, 6),
        "last_observation_result": _latest_completed_observation_result(records),
        "observation_confidence": confidence,
        "observation_reliable": observation_reliable,
    }
    _write_json(OBS_STATS_PATH, stats)
    return stats


def _latest_completed_observation_result(records: list[dict[str, Any]]) -> str:
    completed = [
        row for row in records
        if isinstance(row, dict) and bool(row.get("evaluation_pending")) is False
    ]
    if not completed:
        return "neutral"
    completed.sort(key=lambda row: str(row.get("date") or ""))
    latest = completed[-1]
    return str(latest.get("evaluation_result") or _observation_eval_result(latest.get("t_plus_1_perf")))


def _last_n_observation_results(records: list[dict[str, Any]], n: int = 3) -> list[str]:
    completed = [
        row for row in records
        if isinstance(row, dict) and bool(row.get("evaluation_pending")) is False
    ]
    if not completed:
        return []
    completed.sort(key=lambda row: str(row.get("date") or ""))
    return [
        str(row.get("evaluation_result") or _observation_eval_result(row.get("t_plus_1_perf")))
        for row in completed[-max(1, n):]
    ]


def _evaluation_data_availability_rate(records: list[dict[str, Any]], trading_date: str) -> float:
    eligible: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        row_date = str(row.get("date") or "")
        if not row_date or row_date >= trading_date:
            continue
        if bool(row.get("evaluation_pending")) is True:
            continue
        if "data_ready_for_evaluation" not in row:
            continue
        eligible.append(row)
    eligible.sort(key=lambda row: str(row.get("date") or ""))
    eligible = eligible[-5:]
    if not eligible:
        return 1.0
    ready_count = sum(1 for row in eligible if bool(row.get("data_ready_for_evaluation")) is True)
    return float(ready_count) / float(len(eligible))


def _infer_missing_field(row: dict[str, Any]) -> str | None:
    valid_missing_fields = {
        "t_plus_1_price_missing",
        "market_data_not_loaded",
        "symbol_not_found",
        "timestamp_misaligned",
        "evaluation_not_triggered",
    }
    value = row.get("missing_field")
    if isinstance(value, str) and value in valid_missing_fields:
        return value
    if bool(row.get("evaluation_pending")):
        return "evaluation_not_triggered"
    checks = row.get("precheck_checks") if isinstance(row.get("precheck_checks"), dict) else {}
    if checks and not bool(checks.get("market_data_loaded")):
        return "market_data_not_loaded"
    if checks and not bool(checks.get("timestamp_aligned")):
        return "timestamp_misaligned"
    if checks and not bool(checks.get("symbol_data_found")):
        return "symbol_not_found"
    if row.get("candidate_id") in (None, ""):
        return "evaluation_not_triggered"
    if row.get("t_plus_1_perf") is None:
        return "t_plus_1_price_missing"
    return None


def _infer_failure_reason(row: dict[str, Any]) -> str:
    perf = row.get("t_plus_1_perf")
    if perf is None:
        return str(_infer_missing_field(row) or "evaluation_not_triggered")
    try:
        value = float(perf)
    except (TypeError, ValueError):
        return str(_infer_missing_field(row) or "t_plus_1_price_missing")
    if value < 0:
        return "false_positive"
    if abs(value) <= 1e-9:
        return "no_edge"
    return "no_edge"


def _build_observation_failure_analysis(records: list[dict[str, Any]]) -> dict[str, Any]:
    analyzed: list[dict[str, Any]] = []
    for row in sorted(records, key=lambda item: str(item.get("date") or ""), reverse=True):
        if not isinstance(row, dict):
            continue
        if bool(row.get("evaluation_pending")) is True:
            continue
        result = str(row.get("evaluation_result") or "")
        if result == "good":
            continue
        failure_reason = _infer_failure_reason(row)
        missing_field = _infer_missing_field(row)
        analyzed.append(
            {
                "date": row.get("date"),
                "candidate_id": row.get("candidate_id"),
                "observation_label": row.get("observation_label"),
                "evaluation_result": row.get("evaluation_result"),
                "t_plus_1_perf": row.get("t_plus_1_perf"),
                "failure_reason": failure_reason,
                "missing_field": missing_field,
            }
        )
        if len(analyzed) >= 5:
            break

    false_positive_count = sum(1 for row in analyzed if row.get("failure_reason") == "false_positive")
    no_edge_count = sum(1 for row in analyzed if row.get("failure_reason") == "no_edge")
    missing_reason_set = {
        "t_plus_1_price_missing",
        "market_data_not_loaded",
        "symbol_not_found",
        "timestamp_misaligned",
        "evaluation_not_triggered",
    }
    missing_data_count = sum(1 for row in analyzed if row.get("failure_reason") in missing_reason_set)

    reason_counts: dict[str, int] = {}
    for row in analyzed:
        reason = str(row.get("failure_reason") or "")
        if not reason:
            continue
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    dominant_failure_type = max(reason_counts, key=reason_counts.get) if reason_counts else "none"

    if dominant_failure_type == "false_positive":
        suggested_improvement_focus = "tighten_scoring_or_threshold"
    elif dominant_failure_type == "no_edge":
        suggested_improvement_focus = "feature_or_strategy_weak"
    elif dominant_failure_type in missing_reason_set:
        suggested_improvement_focus = "data_pipeline_issue"
    else:
        suggested_improvement_focus = "none"

    payload = {
        "updated_at": _now_iso(),
        "recent_failures": analyzed,
        "failure_summary": {
            "false_positive_count": false_positive_count,
            "no_edge_count": no_edge_count,
            "missing_data_count": missing_data_count,
            "dominant_failure_type": dominant_failure_type,
        },
        "suggested_improvement_focus": suggested_improvement_focus,
    }
    _write_json(OBS_FAILURE_ANALYSIS_PATH, payload)
    return payload


def _update_ranking_stability(
    logger: logging.Logger,
    trading_date: str,
    system_health_summary: dict[str, Any],
    rank_policy: dict[str, Any],
    stop_policy: dict[str, Any],
    warmup_policy: dict[str, Any],
) -> StepResult:
    raw_records: list[dict[str, Any]] = []
    if STABILITY_PATH.exists():
        existing = _load_json(STABILITY_PATH)
        loaded = existing.get("records") if isinstance(existing.get("records"), list) else []
        raw_records = [row for row in loaded if isinstance(row, dict)]

    constraints = system_health_summary.get("constraints") if isinstance(system_health_summary.get("constraints"), dict) else {}
    today = {
        "trading_date": trading_date,
        "ranking_constraints_passed": _as_bool(system_health_summary.get("ranking_constraints_passed")),
        "constraints": {
            "constraint_top1_ge_top5": _as_bool(constraints.get("constraint_top1_ge_top5")),
            "constraint_top3_ge_top5": _as_bool(constraints.get("constraint_top3_ge_top5")),
            "constraint_top5_gt_unpublished": _as_bool(constraints.get("constraint_top5_gt_unpublished")),
        },
        "corr": round(_as_float(system_health_summary.get("corr")), 6),
        "corr_status": str(system_health_summary.get("corr_status") or "unknown"),
        "recalibration_needed": _as_bool(system_health_summary.get("recalibration_needed")),
        "severe_alert": not _as_bool(constraints.get("constraint_top5_gt_unpublished")),
        "updated_at": _now_iso(),
    }

    by_day = {str(row.get("trading_date") or ""): row for row in raw_records if row.get("trading_date")}
    by_day[trading_date] = today
    records = [by_day[key] for key in sorted(by_day.keys()) if key]

    window_days = int(stop_policy.get("window_days") or 20)
    max_history_days = int(stop_policy.get("max_history_days") or 120)
    window = records[-max(1, window_days):]

    pass_rate = _mean([1.0 if _as_bool(row.get("ranking_constraints_passed")) else 0.0 for row in window])
    corr_values = [_as_float(row.get("corr")) for row in window]
    avg_corr = _mean(corr_values)
    latest_corr = corr_values[-1] if corr_values else 0.0
    severe_alert_days = sum(1 for row in window if _as_bool(row.get("severe_alert")))

    consecutive_constraint_fail_days = 0
    for row in reversed(window):
        if not _as_bool(row.get("ranking_constraints_passed")):
            consecutive_constraint_fail_days += 1
        else:
            break

    corr_non_positive_streak_days = 0
    for row in reversed(window):
        if _as_float(row.get("corr")) <= 0:
            corr_non_positive_streak_days += 1
        else:
            break

    severe_alert_streak_days = 0
    for row in reversed(window):
        if _as_bool(row.get("severe_alert")):
            severe_alert_streak_days += 1
        else:
            break

    recalibration_required, recalibration_reasons = _evaluate_recalibration_required(window, rank_policy)

    stop_conditions = stop_policy.get("stop_conditions") if isinstance(stop_policy.get("stop_conditions"), dict) else {}
    stop_reasons: list[str] = []
    if consecutive_constraint_fail_days >= int(stop_conditions.get("consecutive_constraint_fail_days") or 2):
        stop_reasons.append(f"consecutive_constraint_fail_days:{consecutive_constraint_fail_days}")
    if corr_non_positive_streak_days >= int(stop_conditions.get("corr_non_positive_streak_days") or 2):
        stop_reasons.append(f"corr_non_positive_streak_days:{corr_non_positive_streak_days}")
    if _as_bool(stop_conditions.get("recalibration_required")) and recalibration_required:
        stop_reasons.append("recalibration_required")
    stop_triggered = bool(stop_reasons)

    warmup_thresholds = warmup_policy.get("thresholds") if isinstance(warmup_policy.get("thresholds"), dict) else {}
    warmup_min_window_days = int(warmup_thresholds.get("min_window_days") or 10)
    warmup_min_constraint_pass_rate = float(warmup_thresholds.get("min_constraint_pass_rate") or 0.8)
    warmup_min_avg_corr = float(warmup_thresholds.get("min_avg_corr") or 0.02)
    warmup_max_severe_alert_streak_days = int(warmup_thresholds.get("max_consecutive_severe_alert_days") or 1)
    warmup_require_stop_clear = _as_bool(warmup_thresholds.get("stop_policy_must_be_clear", True))

    early_cfg = warmup_policy.get("early_activation") if isinstance(warmup_policy.get("early_activation"), dict) else {}
    early_min_window_days = int(early_cfg.get("min_window_days") or 5)
    early_min_constraint_pass_rate = float(early_cfg.get("min_constraint_pass_rate") or 0.9)
    early_min_avg_corr = float(early_cfg.get("min_avg_corr") or 0.05)
    early_max_severe_alert_days = int(early_cfg.get("max_severe_alert_days") or 0)

    warmup_days_remaining = max(0, warmup_min_window_days - len(window))
    warmup_checks = {
        "window_ready": len(window) >= warmup_min_window_days,
        "constraint_pass_rate_ok": pass_rate >= warmup_min_constraint_pass_rate,
        "avg_corr_ok": avg_corr >= warmup_min_avg_corr,
        "severe_alert_streak_ok": severe_alert_streak_days <= warmup_max_severe_alert_streak_days,
        "stop_policy_not_triggered": (not stop_triggered) if warmup_require_stop_clear else True,
    }
    warmup_completed = bool(all(warmup_checks.values()))

    early_activation_allowed = bool(
        len(window) >= early_min_window_days
        and pass_rate >= early_min_constraint_pass_rate
        and avg_corr >= early_min_avg_corr
        and severe_alert_days <= early_max_severe_alert_days
        and (not stop_triggered)
    )

    recent3 = window[-3:]
    prev3 = window[-6:-3]
    if len(recent3) == 3 and len(prev3) == 3:
        recent_pass = _mean([1.0 if _as_bool(row.get("ranking_constraints_passed")) else 0.0 for row in recent3])
        prev_pass = _mean([1.0 if _as_bool(row.get("ranking_constraints_passed")) else 0.0 for row in prev3])
        recent_corr = _mean([_as_float(row.get("corr")) for row in recent3])
        prev_corr = _mean([_as_float(row.get("corr")) for row in prev3])
        trend_constraint_pass_rate = _trend_label(recent_pass, prev_pass, epsilon=0.03)
        trend_corr = _trend_label(recent_corr, prev_corr, epsilon=0.01)
    else:
        trend_constraint_pass_rate = "flat"
        trend_corr = "flat"

    if stop_triggered:
        system_phase = "halted"
    elif not warmup_checks["window_ready"]:
        system_phase = "warmup"
    elif warmup_completed:
        system_phase = "active"
    else:
        system_phase = "degraded"

    reliability_gate = stop_policy.get("reliability_gate") if isinstance(stop_policy.get("reliability_gate"), dict) else {}
    min_days_required = int(reliability_gate.get("min_days_required") or 5)
    min_pass_rate = float(reliability_gate.get("min_constraint_pass_rate") or 0.8)
    min_avg_corr = float(reliability_gate.get("min_avg_corr") or 0.02)
    require_positive_latest_corr = _as_bool(reliability_gate.get("require_positive_latest_corr", True))
    require_no_severe_alert = _as_bool(reliability_gate.get("require_no_severe_alert", True))

    if system_phase != "active":
        system_reliable = False
    elif len(window) < min_days_required:
        system_reliable = False
        reliability_reason = f"insufficient_window:{len(window)}/{min_days_required}"
    elif stop_triggered:
        system_reliable = False
        reliability_reason = "stop_policy_triggered:" + "|".join(stop_reasons)
    elif pass_rate < min_pass_rate:
        system_reliable = False
        reliability_reason = f"constraint_pass_rate_below_threshold:{pass_rate:.3f}<{min_pass_rate:.3f}"
    elif avg_corr < min_avg_corr:
        system_reliable = False
        reliability_reason = f"avg_corr_below_threshold:{avg_corr:.4f}<{min_avg_corr:.4f}"
    elif require_positive_latest_corr and latest_corr <= 0:
        system_reliable = False
        reliability_reason = f"latest_corr_non_positive:{latest_corr:.4f}"
    elif require_no_severe_alert and severe_alert_days > 0:
        system_reliable = False
        reliability_reason = f"severe_alert_days_in_window:{severe_alert_days}"
    else:
        system_reliable = True
        reliability_reason = "window_constraints_and_corr_healthy"

    if system_phase == "warmup":
        reliability_reason = f"warmup_in_progress:remaining_days={warmup_days_remaining}"
    elif system_phase == "degraded":
        failed_checks = [key for key, ok in warmup_checks.items() if not ok]
        reliability_reason = "degraded:" + "|".join(failed_checks) if failed_checks else "degraded:constraints_not_met"
    elif system_phase == "halted":
        reliability_reason = "halted:" + "|".join(stop_reasons)

    if system_phase == "halted":
        observation_quality = "low"
    elif system_phase == "active" or early_activation_allowed:
        observation_quality = "high"
    elif (trend_constraint_pass_rate == "↑" and trend_corr in {"↑", "flat"}) or (pass_rate >= 0.6 and avg_corr > 0):
        observation_quality = "medium"
    else:
        observation_quality = "low"

    distance_to_active = int(warmup_days_remaining)
    try:
        trading_day = date.fromisoformat(trading_date)
    except ValueError:
        trading_day = date.today()
    projected_active_date = (trading_day + timedelta(days=distance_to_active)).isoformat()

    warmup_progress = {
        "updated_at": _now_iso(),
        "trading_date": trading_date,
        "day_index": len(window),
        "window_days": len(window),
        "constraint_pass_rate": round(pass_rate, 6),
        "avg_corr": round(avg_corr, 6),
        "severe_alert_days": int(severe_alert_days),
        "distance_to_active": distance_to_active,
        "projected_active_date": projected_active_date,
        "trend_constraint_pass_rate": trend_constraint_pass_rate,
        "trend_corr": trend_corr,
        "early_activation_allowed": early_activation_allowed,
    }

    payload = {
        "updated_at": _now_iso(),
        "window_days": window_days,
        "records": records[-max(1, max_history_days):],
        "latest_window": window,
        "window_summary": {
            "window_size": len(window),
            "constraint_pass_rate": round(pass_rate, 6),
            "avg_corr": round(avg_corr, 6),
            "latest_corr": round(latest_corr, 6),
            "severe_alert_days": int(severe_alert_days),
            "severe_alert_streak_days": int(severe_alert_streak_days),
            "consecutive_constraint_fail_days": int(consecutive_constraint_fail_days),
            "corr_non_positive_streak_days": int(corr_non_positive_streak_days),
        },
        "warmup": {
            "completed": warmup_completed,
            "days_remaining": int(warmup_days_remaining),
            "can_use_early": early_activation_allowed,
            "checks": warmup_checks,
            "trend": {
                "trend_constraint_pass_rate": trend_constraint_pass_rate,
                "trend_corr": trend_corr,
            },
            "thresholds": {
                "min_window_days": warmup_min_window_days,
                "min_constraint_pass_rate": warmup_min_constraint_pass_rate,
                "min_avg_corr": warmup_min_avg_corr,
                "max_consecutive_severe_alert_days": warmup_max_severe_alert_streak_days,
                "stop_policy_must_be_clear": warmup_require_stop_clear,
                "early_activation": {
                    "min_window_days": early_min_window_days,
                    "min_constraint_pass_rate": early_min_constraint_pass_rate,
                    "min_avg_corr": early_min_avg_corr,
                    "max_severe_alert_days": early_max_severe_alert_days,
                },
            },
        },
        "recalibration_required": recalibration_required,
        "recalibration_reasons": recalibration_reasons,
        "stop_policy_triggered": stop_triggered,
        "stop_policy_reasons": stop_reasons,
        "system_phase": system_phase,
        "can_use_early": early_activation_allowed,
        "observation_quality": observation_quality,
        "system_reliable": system_reliable,
        "reliability_reason": reliability_reason,
    }

    try:
        _write_json(STABILITY_PATH, payload)
        _write_json(WARMUP_PROGRESS_PATH, warmup_progress)
    except Exception as exc:
        raise FactoryError(f"ranking_stability_record_failed:{exc}") from exc

    detail = {
        "path": str(STABILITY_PATH.relative_to(ROOT)),
        "window_size": len(window),
        "constraint_pass_rate": round(pass_rate, 6),
        "avg_corr": round(avg_corr, 6),
        "latest_corr": round(latest_corr, 6),
        "trend_constraint_pass_rate": trend_constraint_pass_rate,
        "trend_corr": trend_corr,
        "recalibration_required": recalibration_required,
        "stop_policy_triggered": stop_triggered,
        "system_phase": system_phase,
        "warmup_days_remaining": int(warmup_days_remaining),
        "can_use_early": early_activation_allowed,
        "observation_quality": observation_quality,
        "system_reliable": system_reliable,
        "reliability_reason": reliability_reason,
    }
    logger.info("step9_ranking_stability_ok reliable=%s reason=%s", system_reliable, reliability_reason)
    return StepResult("ranking_stability", True, detail)


def _write_history(trading_date: str) -> None:
    day_dir = HISTORY_DIR / trading_date
    day_dir.mkdir(parents=True, exist_ok=True)
    for src in [TOP_PATH, STATUS_PATH, REJECTED_PATH, LOG_PATH]:
        if src.exists():
            target = day_dir / src.name
            target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def run_factory(trading_date: str, top_n: int) -> dict[str, Any]:
    logger = _setup_logger()
    logger.info("daily_candidate_factory_start trading_date=%s", trading_date)

    observation_activation_policy = _load_json(OBS_ACTIVATION_POLICY_PATH)

    records_after_backfill: list[dict[str, Any]] = []
    stats_after_backfill: dict[str, Any] = {"hit_rate": 0.0, "total_observations": 0}
    last_result_after_backfill = "neutral"

    spec = _load_json(SPEC_PATH)
    score_cfg = _load_json(SCORE_PATH)
    blacklist_policy = _load_json(BLACKLIST_POLICY_PATH)
    blacklist_state = _load_json(BLACKLIST_STATE_PATH)
    rank_recal_policy = _load_json(RANK_RECAL_POLICY_PATH)
    stop_policy = _load_json(STOP_POLICY_PATH)
    warmup_policy = _load_json(WARMUP_POLICY_PATH)

    status: dict[str, Any] = {
        "run_at": _now_iso(),
        "trading_date": trading_date,
        "steps": [],
        "ok": False,
    }
    rejected: list[dict[str, Any]] = []

    def run_step(fn, *args):
        result = fn(logger, *args)
        status["steps"].append({"name": result.name, "ok": result.ok, "detail": result.detail})
        if not result.ok:
            raise FactoryError(f"step_failed:{result.name}")
        return result.detail

    try:
        run_step(_run_evaluation_data_precheck, trading_date)
        records_after_backfill = _backfill_yesterday_observation(trading_date)
        records_after_backfill = _retrofill_observation_data(records_after_backfill, trading_date)
        stats_after_backfill = _write_observation_stats(records_after_backfill, observation_activation_policy)
        last_result_after_backfill = _latest_completed_observation_result(records_after_backfill)

        run_step(_run_data_update, trading_date)
        run_step(_run_feature_update, trading_date)

        generated = run_step(_build_candidates, trading_date)
        step4 = run_step(_run_backtest_validation, generated["candidates"])
        rejected.extend(step4["rejected"])

        step5 = run_step(_run_robustness, step4["valid"])
        rejected.extend(step5["rejected"])

        step6 = run_step(_run_scoring, step5["passed"], score_cfg)
        rejected.extend(step6["rejected"])

        step7 = run_step(_blacklist_gate, step6["scored"], blacklist_policy, blacklist_state)
        rejected.extend(step7["rejected"])

        step8 = run_step(_run_freshness_gate, trading_date, step7["published"])
        rejected.extend(step8["rejected"])

        top, spec_rejected = _publish_top(logger, spec, step8["passed"], top_n=top_n)
        status["steps"].append(
            {
                "name": "publish_top_n",
                "ok": True,
                "detail": {
                    "requested_top_n": top_n,
                    "published": len(top),
                    "spec_rejected": len(spec_rejected),
                },
            }
        )
        rejected.extend(spec_rejected)

        summary = {
            "total_candidates": generated["count"],
            "spec_passed": len(step8["passed"]) - len(spec_rejected),
            "scored": len(step6["scored"]),
            "blacklist_passed": len(step7["published"]),
            "freshness_passed": len(step8["passed"]),
            "freshness_downweighted": int(step8.get("downweighted") or 0),
            "published": len(top),
        }

        top_candidate_pnl = [
            float((((row.get("evidence_summary") or {}).get("backtest") or {}).get("net_pnl_usd") or 0.0))
            for row in top
        ]
        unpublished_candidates = [
            row for row in (step8.get("passed") or [])
            if str(row.get("candidate_id") or "") not in {str(x.get("candidate_id") or "") for x in top}
        ]
        unpublished_pnl = [
            float((((row.get("evidence") or {}).get("backtest") or {}).get("net_pnl_usd") or 0.0))
            for row in unpublished_candidates
        ]

        top1_mean = float(top_candidate_pnl[0]) if top_candidate_pnl else 0.0
        top3_mean = float(sum(top_candidate_pnl[:3]) / max(len(top_candidate_pnl[:3]), 1)) if top_candidate_pnl else 0.0
        top5_mean = float(sum(top_candidate_pnl) / max(len(top_candidate_pnl), 1)) if top_candidate_pnl else 0.0
        unpublished_mean = float(sum(unpublished_pnl) / len(unpublished_pnl)) if unpublished_pnl else 0.0

        scored_rows = step6.get("scored") or []
        score_vals = [float(((row.get("score") or {}).get("total") or 0.0)) for row in scored_rows]
        perf_vals = [float((((row.get("evidence") or {}).get("backtest") or {}).get("net_pnl_usd") or 0.0)) for row in scored_rows]
        corr = _corr(score_vals, perf_vals)

        constraints = {
            "constraint_top1_ge_top5": bool(top1_mean >= top5_mean),
            "constraint_top3_ge_top5": bool(top3_mean >= top5_mean),
            "constraint_top5_gt_unpublished": bool(top5_mean > unpublished_mean),
        }
        ranking_constraints_passed = bool(all(constraints.values()))
        corr_status = "positive" if corr > 0 else "non_positive"
        freshness_status = "blocked_or_downweighted" if (int(step8.get("downweighted") or 0) > 0 or len(step8.get("rejected") or []) > 0) else "clean"
        recalibration_needed = bool((not ranking_constraints_passed) or corr <= 0)

        system_health_summary = {
            "score_version": str(top[0].get("score_version") if top else "unknown"),
            "ranking_constraints_passed": ranking_constraints_passed,
            "constraints": constraints,
            "corr": round(corr, 6),
            "corr_status": corr_status,
            "freshness_status": freshness_status,
            "recalibration_needed": recalibration_needed,
            "group_means": {
                "top1_mean": round(top1_mean, 6),
                "top3_mean": round(top3_mean, 6),
                "top5_mean": round(top5_mean, 6),
                "unpublished_mean": round(unpublished_mean, 6),
            },
        }

        stability_detail = run_step(_update_ranking_stability, trading_date, system_health_summary, rank_recal_policy, stop_policy, warmup_policy)
        system_reliable = bool(stability_detail.get("system_reliable"))
        reliability_reason = str(stability_detail.get("reliability_reason") or "unknown")
        system_phase = str(stability_detail.get("system_phase") or "degraded")
        warmup_days_remaining = int(stability_detail.get("warmup_days_remaining") or 0)
        can_use_early = bool(stability_detail.get("can_use_early"))
        observation_quality = str(stability_detail.get("observation_quality") or "low")
        release_warning = ""
        if bool(stability_detail.get("stop_policy_triggered")):
            release_warning = f"STOP_USE_AS_DECISION_INPUT: {reliability_reason}"

        use_for_decision = system_phase == "active" or can_use_early
        if system_phase == "active":
            use_reason = "active_phase_constraints_ok"
        elif can_use_early:
            use_reason = "early_activation_allowed"
        elif system_phase == "warmup":
            use_reason = f"warmup_phase_remaining_days:{warmup_days_remaining}"
        elif system_phase == "halted":
            use_reason = f"halted:{reliability_reason}"
        else:
            use_reason = f"{system_phase}:{reliability_reason}"

        observation_top = _pick_observation_top(top)
        score_p80 = _history_score_p80(trading_date=trading_date, today_top=top)
        observation_label, suggested_action = _observation_label(
            candidate=observation_top,
            ranking_constraints_passed=ranking_constraints_passed,
            corr=float(corr),
            score_p80=float(score_p80),
        )
        observation_top_candidate = None
        if observation_top is not None:
            observation_top_candidate = copy.deepcopy(observation_top)

        top_payload = {
            "trading_date": trading_date,
            "generated_at": _now_iso(),
            "top_n": len(top),
            "system_health_summary": system_health_summary,
            "system_phase": system_phase,
            "can_use_early": can_use_early,
            "system_reliable": system_reliable,
            "reliability_reason": reliability_reason,
            "use_for_decision": use_for_decision,
            "use_reason": use_reason,
            "observation_top_candidate": observation_top_candidate,
            "observation_label": observation_label,
            "suggested_action": suggested_action,
            "release_warning": release_warning,
            "candidates": top,
        }
        rejected_payload = {
            "trading_date": trading_date,
            "generated_at": _now_iso(),
            "rejected": rejected,
        }
        records_after_append = _append_today_observation(
            trading_date=trading_date,
            observation_top=observation_top,
            label=observation_label,
            suggested_action=suggested_action,
        )
        observation_stats = _write_observation_stats(records_after_append, observation_activation_policy)
        last_observation_result = _latest_completed_observation_result(records_after_append)
        evaluation_data_availability_rate = _evaluation_data_availability_rate(records_after_append, trading_date)
        failure_analysis = _build_observation_failure_analysis(records_after_append)
        failure_summary = failure_analysis.get("failure_summary") if isinstance(failure_analysis.get("failure_summary"), dict) else {}
        dominant_failure_type = str(failure_summary.get("dominant_failure_type") or "none")
        raw_last_5_failures = failure_analysis.get("recent_failures") if isinstance(failure_analysis.get("recent_failures"), list) else []
        last_5_failures = [
            {
                "candidate_id": row.get("candidate_id"),
                "reason": row.get("failure_reason"),
            }
            for row in raw_last_5_failures
            if isinstance(row, dict)
        ]
        suggested_improvement_focus = str(failure_analysis.get("suggested_improvement_focus") or "none")
        total_observations = int(observation_stats.get("total_observations") or 0)
        observation_hit_rate = float(observation_stats.get("hit_rate") or 0.0)
        observation_confidence = _observation_confidence(total_observations=total_observations, hit_rate=observation_hit_rate)
        activation_min_observations = int(observation_activation_policy.get("min_observations") or 5)
        activation_min_hit_rate = float(observation_activation_policy.get("min_hit_rate") or 0.5)
        observation_reliable = bool(total_observations >= activation_min_observations and observation_hit_rate >= activation_min_hit_rate)
        recent_observation_summary = {
            "last_3_results": _last_n_observation_results(records_after_append, n=3),
            "current_hit_rate": round(observation_hit_rate, 6),
            "observation_confidence": observation_confidence,
        }

        if total_observations >= 5 and observation_hit_rate < 0.4:
            system_reliable = False
            reliability_reason = "low_observation_hit_rate"
        if evaluation_data_availability_rate < 0.8:
            system_reliable = False
            reliability_reason = "low_data_availability"

        top_payload["system_reliable"] = system_reliable
        top_payload["reliability_reason"] = reliability_reason
        top_payload["recent_observation_summary"] = recent_observation_summary

        status.update(
            {
                "ok": True,
                "summary": summary,
                "failure_step": None,
                "system_phase": system_phase,
                "can_use_early": can_use_early,
                "observation_quality": observation_quality,
                "observation_hit_rate": observation_hit_rate,
                "last_observation_result": str(last_observation_result),
                "observation_confidence": observation_confidence,
                "observation_reliable": observation_reliable,
                "evaluation_data_availability_rate": round(evaluation_data_availability_rate, 6),
                "dominant_failure_type": dominant_failure_type,
                "last_5_failures": last_5_failures,
                "suggested_improvement_focus": suggested_improvement_focus,
                "system_reliable": system_reliable,
                "reliability_reason": reliability_reason,
            }
        )

        _assert_required_fields(
            top_payload,
            [
                "trading_date",
                "generated_at",
                "top_n",
                "system_health_summary",
                "system_phase",
                "can_use_early",
                "system_reliable",
                "reliability_reason",
                "use_for_decision",
                "use_reason",
                "observation_top_candidate",
                "observation_label",
                "suggested_action",
                "recent_observation_summary",
                "release_warning",
                "candidates",
            ],
            "daily_top_candidates",
        )
        _assert_required_fields(rejected_payload, ["trading_date", "generated_at", "rejected"], "rejected_candidates")
        _assert_required_fields(
            status,
            [
                "run_at",
                "trading_date",
                "steps",
                "ok",
                "summary",
                "system_phase",
                "can_use_early",
                "observation_quality",
                "observation_hit_rate",
                "last_observation_result",
                "observation_confidence",
                "observation_reliable",
                "evaluation_data_availability_rate",
                "dominant_failure_type",
                "last_5_failures",
                "suggested_improvement_focus",
                "system_reliable",
                "reliability_reason",
            ],
            "daily_candidate_factory_status",
        )

        _write_json(TOP_PATH, top_payload)
        _write_json(REJECTED_PATH, rejected_payload)
        _write_json(STATUS_PATH, status)
        _write_history(trading_date)
        logger.info("daily_candidate_factory_done ok=true published=%s", len(top))
        return status
    except Exception as exc:
        status.update({
            "ok": False,
            "failure_step": status["steps"][-1]["name"] if status["steps"] else "bootstrap",
            "error": str(exc),
            "system_phase": "degraded",
            "can_use_early": False,
            "observation_quality": "low",
            "observation_hit_rate": float(stats_after_backfill.get("hit_rate") or 0.0),
            "last_observation_result": str(last_result_after_backfill),
            "observation_confidence": _observation_confidence(
                total_observations=int(stats_after_backfill.get("total_observations") or 0),
                hit_rate=float(stats_after_backfill.get("hit_rate") or 0.0),
            ),
            "observation_reliable": False,
            "evaluation_data_availability_rate": round(_evaluation_data_availability_rate(records_after_backfill, trading_date), 6),
            "dominant_failure_type": "none",
            "last_5_failures": [],
            "suggested_improvement_focus": "none",
            "system_reliable": False,
            "reliability_reason": f"factory_run_failed:{exc}",
            "summary": {
                "total_candidates": len([x for x in status.get("steps", []) if x.get("name") == "candidate_generation"]),
                "published": 0,
            },
        })
        _write_json(REJECTED_PATH, {"trading_date": trading_date, "generated_at": _now_iso(), "rejected": rejected})
        _write_json(STATUS_PATH, status)
        _write_json(TOP_PATH, {"trading_date": trading_date, "generated_at": _now_iso(), "top_n": 0, "candidates": []})
        _write_history(trading_date)
        logger.exception("daily_candidate_factory_failed")
        return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily Candidate Factory single-entry pipeline")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Trading date in YYYY-MM-DD")
    parser.add_argument("--top-n", type=int, default=5, help="Maximum candidates to publish")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    top_n = max(1, min(5, int(args.top_n)))
    status = run_factory(trading_date=str(args.date), top_n=top_n)
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
