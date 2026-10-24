#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "scripts" / "daily_candidate_factory.py"
TARGET_CFG = ROOT / "config" / "candidate_factory" / "score_calibration_target.json"
RANK_OBJ_CFG = ROOT / "config" / "candidate_factory" / "score_ranking_objective.json"
LEGACY_SCORE_CFG = ROOT / "config" / "candidate_factory" / "scoring_function.json"
RECAL_SCORE_CFG = ROOT / "config" / "candidate_factory" / "scoring_function_recalibrated.json"
OUT_DIR = ROOT / "runtime" / "state" / "candidate_factory" / "calibration"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _is_trading_day(day: date) -> bool:
    try:
        from libs.market_calendar import is_trading_day

        return bool(is_trading_day(day))
    except Exception:
        return day.weekday() < 5


def _recent_trading_days(count: int, end_day: date) -> list[str]:
    days: list[str] = []
    cursor = end_day
    while len(days) < count:
        if _is_trading_day(cursor):
            days.append(cursor.isoformat())
        cursor = cursor - timedelta(days=1)
    days.sort()
    return days


def _run_factory(day: str) -> None:
    import subprocess

    subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), str(FACTORY), "--date", day, "--top-n", "5"],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )


def _corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = sum((x - mx) ** 2 for x in xs)
    deny = sum((y - my) ** 2 for y in ys)
    if denx <= 0 or deny <= 0:
        return 0.0
    return num / ((denx ** 0.5) * (deny ** 0.5))


def _future_perf(row: dict[str, Any], cfg: dict[str, Any]) -> float:
    coeff = cfg.get("penalty_coefficients") or {}
    dd_coeff = float(coeff.get("dd_penalty_coeff") or 0.8)
    cost_coeff = float(coeff.get("cost_penalty_coeff") or 0.7)
    stability_coeff = float(coeff.get("stability_penalty_coeff") or 0.6)

    metrics = row.get("raw_metrics") or {}
    net = float(metrics.get("net_pnl_usd") or 0.0)
    dd = float(metrics.get("max_drawdown_pct") or 0.0)
    legacy = row.get("legacy_score") or row.get("score") or {}
    rank_basis = legacy.get("rank_basis") if isinstance(legacy.get("rank_basis"), dict) else {}
    cost_ratio = float(rank_basis.get("cost_sensitivity_ratio") or 0.0)
    sub = legacy.get("sub_scores") if isinstance(legacy.get("sub_scores"), dict) else {}
    stability = float(sub.get("stability") or 0.0)
    base = abs(net)
    return net - dd_coeff * base * dd - cost_coeff * base * cost_ratio - stability_coeff * base * (1.0 - stability)


def _build_daily_index(day: str) -> dict[str, dict[str, Any]]:
    history_root = ROOT / "runtime" / "state" / "candidate_factory" / "history" / day
    status = _load_json(history_root / "daily_candidate_factory_status.json")
    steps = status.get("steps") if isinstance(status.get("steps"), list) else []
    scored: list[dict[str, Any]] = []
    black_rej: list[dict[str, Any]] = []
    fresh_rej: list[dict[str, Any]] = []
    published = _load_json(history_root / "daily_top_candidates.json")
    pub_ids = {str(x.get("candidate_id") or "") for x in (published.get("candidates") or []) if isinstance(x, dict)}

    for step in steps:
        if not isinstance(step, dict):
            continue
        name = step.get("name")
        detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
        if name == "scoring":
            raw = detail.get("scored") if isinstance(detail.get("scored"), list) else []
            scored = [x for x in raw if isinstance(x, dict)]
        if name == "blacklist_gate":
            raw = detail.get("rejected") if isinstance(detail.get("rejected"), list) else []
            black_rej = [x for x in raw if isinstance(x, dict)]
        if name == "freshness_gate":
            raw = detail.get("rejected") if isinstance(detail.get("rejected"), list) else []
            fresh_rej = [x for x in raw if isinstance(x, dict)]

    by_id: dict[str, dict[str, Any]] = {}
    for row in scored:
        cid = str(row.get("candidate_id") or "")
        if not cid:
            continue
        state = "published" if cid in pub_ids else "unpublished"
        by_id[cid] = {
            "date": day,
            "candidate_id": cid,
            "candidate_signature": str(row.get("candidate_signature") or ""),
            "strategy_family": str(row.get("strategy_family") or ""),
            "candidate_type": str(row.get("candidate_type") or ""),
            "legacy_score": row.get("legacy_score") or {},
            "recalibrated_score": row.get("recalibrated_score") or row.get("score") or {},
            "score": row.get("score") or {},
            "raw_metrics": row.get("raw_metrics") or {},
            "spec_status": "pass",
            "blacklist_status": "pass",
            "freshness_status": "pass",
            "publish_status": state,
        }

    for row in black_rej:
        cid = str(row.get("candidate_id") or "")
        if cid in by_id:
            by_id[cid]["blacklist_status"] = str(row.get("reason") or "blocked")
            by_id[cid]["publish_status"] = "blocked"

    for row in fresh_rej:
        cid = str(row.get("candidate_id") or "")
        if cid in by_id:
            by_id[cid]["freshness_status"] = str(row.get("reason") or "blocked")
            by_id[cid]["publish_status"] = "blocked"

    return by_id


def _calc_target(perf_t1: float | None, perf_t3: float | None, perf_t5: float | None, cfg: dict[str, Any]) -> float | None:
    if perf_t1 is None and perf_t3 is None and perf_t5 is None:
        return None
    ws = (cfg.get("calibration_target") or {}).get("weights") or {}
    w1 = float(ws.get("w_t1") or 0.4)
    w3 = float(ws.get("w_t3") or 0.35)
    w5 = float(ws.get("w_t5") or 0.25)
    values = [perf_t1, perf_t3, perf_t5]
    weights = [w1, w3, w5]
    num = 0.0
    den = 0.0
    for value, weight in zip(values, weights):
        if value is None:
            continue
        num += float(value) * weight
        den += weight
    return (num / den) if den > 0 else None


def _rank_group_stats(rows: list[dict[str, Any]], score_key: str) -> dict[str, float]:
    if not rows:
        return {"top1": 0.0, "top3": 0.0, "top5": 0.0, "unpublished": 0.0}
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_date.setdefault(str(row.get("date") or ""), []).append(row)

    top1_vals: list[float] = []
    top3_vals: list[float] = []
    top5_vals: list[float] = []
    unpub_vals: list[float] = []

    for _, day_rows in by_date.items():
        sorted_rows = sorted(day_rows, key=lambda x: float((x.get(score_key) or {}).get("total") or 0.0), reverse=True)
        top = sorted_rows[:5]
        if top:
            top1_vals.append(float(top[0].get("calibration_target") or 0.0))
            top3_vals.append(sum(float(x.get("calibration_target") or 0.0) for x in top[:3]) / max(len(top[:3]), 1))
            top5_vals.append(sum(float(x.get("calibration_target") or 0.0) for x in top) / len(top))
        unpub = [x for x in day_rows if str(x.get("publish_status") or "") != "published"]
        if unpub:
            unpub_vals.append(sum(float(x.get("calibration_target") or 0.0) for x in unpub) / len(unpub))

    mean = lambda values: (sum(values) / len(values)) if values else 0.0
    return {"top1": mean(top1_vals), "top3": mean(top3_vals), "top5": mean(top5_vals), "unpublished": mean(unpub_vals)}


def _transform(value: float, mode: str) -> float:
    clipped = min(1.0, max(0.0, value))
    if mode == "square":
        return clipped * clipped
    if mode == "sqrt":
        return clipped ** 0.5
    return clipped


def _apply_recal_score(rows_subset: list[dict[str, Any]], weights: dict[str, float], direction: dict[str, str], transform_map: dict[str, str]) -> None:
    for row in rows_subset:
        sub = row.get("legacy_sub_scores") or {}
        total = 0.0
        for feature, weight in weights.items():
            value = float(sub.get(feature) or 0.0)
            if direction.get(feature) == "negative":
                value = 1.0 - value
            value = _transform(value, transform_map.get(feature, "identity"))
            total += float(weight) * value
        row["recalibrated_score"] = {"total": round(100.0 * total, 4)}


def _objective_value(rows_subset: list[dict[str, Any]], ranking_cfg: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    if not rows_subset:
        return -1e9, {"corr": 0.0, "groups": {"top1": 0.0, "top3": 0.0, "top5": 0.0, "unpublished": 0.0}, "penalty": 0.0}
    xs = [float((x.get("recalibrated_score") or {}).get("total") or 0.0) for x in rows_subset]
    ys = [float(x.get("calibration_target") or 0.0) for x in rows_subset]
    corr = _corr(xs, ys)
    groups = _rank_group_stats(rows_subset, "recalibrated_score")

    penalties = ((ranking_cfg.get("optimization") or {}).get("penalties") or {})
    p_top1 = float(penalties.get("top1_lt_top5") or 3.0)
    p_top3 = float(penalties.get("top3_lt_top5") or 2.5)
    p_top5 = float(penalties.get("top5_lte_unpublished") or 1.5)
    p_corr = float(penalties.get("corr_non_positive") or 2.0)

    penalty = 0.0
    constraints_cfg = (ranking_cfg.get("constraints") or {})
    hard_fail = set(constraints_cfg.get("hard_failure_if_violated") or [])

    c_top1 = bool(groups["top1"] >= groups["top5"])
    c_top3 = bool(groups["top3"] >= groups["top5"])
    c_top5 = bool(groups["top5"] > groups["unpublished"])

    if "top1_mean_gte_top5_mean" in hard_fail and not c_top1:
        return -1e9, {"corr": round(corr, 6), "groups": groups, "penalty": 1e9, "ranking_margin": -1e9, "hard_failed": "top1_mean_gte_top5_mean"}
    if "top3_mean_gte_top5_mean" in hard_fail and not c_top3:
        return -1e9, {"corr": round(corr, 6), "groups": groups, "penalty": 1e9, "ranking_margin": -1e9, "hard_failed": "top3_mean_gte_top5_mean"}
    if groups["top1"] < groups["top5"]:
        penalty += p_top1 * (groups["top5"] - groups["top1"])
    if groups["top3"] < groups["top5"]:
        penalty += p_top3 * (groups["top5"] - groups["top3"])
    if groups["top5"] <= groups["unpublished"]:
        penalty += p_top5 * (groups["unpublished"] - groups["top5"] + 1e-6)
    if corr <= 0:
        penalty += p_corr * abs(corr - 0.01)

    ranking_margin = (groups["top1"] - groups["top5"]) + 0.5 * (groups["top3"] - groups["top5"]) + 0.5 * (groups["top5"] - groups["unpublished"])
    value = corr + 0.001 * ranking_margin - 0.001 * penalty
    detail = {
        "corr": round(corr, 6),
        "groups": groups,
        "penalty": round(penalty, 6),
        "ranking_margin": round(ranking_margin, 6),
        "constraints": {
            "top1_mean_gte_top5_mean": c_top1,
            "top3_mean_gte_top5_mean": c_top3,
            "top5_mean_gt_unpublished_mean": c_top5,
        },
    }
    return value, detail


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recalibrate candidate scoring weights")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--train-days", type=int, default=6)
    parser.add_argument("--end-date", default=date.today().isoformat())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_cfg = _load_json(TARGET_CFG)
    ranking_cfg = _load_json(RANK_OBJ_CFG)
    legacy_cfg = _load_json(LEGACY_SCORE_CFG)

    days = _recent_trading_days(count=max(2, int(args.days)), end_day=date.fromisoformat(str(args.end_date)))
    for day in days:
        _run_factory(day)

    by_day: dict[str, dict[str, dict[str, Any]]] = {}
    for day in days:
        by_day[day] = _build_daily_index(day)

    rows: list[dict[str, Any]] = []
    for idx, day in enumerate(days):
        current = by_day[day]
        for candidate in current.values():
            sig = str(candidate.get("candidate_signature") or "")

            def future_perf(offset: int) -> float | None:
                target_idx = idx + offset
                if target_idx >= len(days):
                    return None
                target_day = days[target_idx]
                target_rows = by_day.get(target_day, {})
                for future in target_rows.values():
                    if str(future.get("candidate_signature") or "") == sig:
                        return _future_perf(future, target_cfg)
                return None

            perf_t1 = future_perf(1)
            perf_t3 = future_perf(3)
            perf_t5 = future_perf(5)
            calibration_target = _calc_target(perf_t1, perf_t3, perf_t5, target_cfg)

            row = {
                "candidate_id": candidate.get("candidate_id"),
                "candidate_signature": sig,
                "date": day,
                "family_type": {
                    "strategy_family": candidate.get("strategy_family"),
                    "candidate_type": candidate.get("candidate_type"),
                },
                "legacy_sub_scores": (candidate.get("legacy_score") or {}).get("sub_scores") or {},
                "legacy_score": candidate.get("legacy_score") or {},
                "recalibrated_score": candidate.get("recalibrated_score") or {},
                "spec_status": candidate.get("spec_status"),
                "blacklist_status": candidate.get("blacklist_status"),
                "freshness_status": candidate.get("freshness_status"),
                "publish_status": candidate.get("publish_status"),
                "perf_t1": perf_t1,
                "perf_t3": perf_t3,
                "perf_t5": perf_t5,
                "calibration_target": calibration_target,
            }
            rows.append(row)

    valid_rows = [x for x in rows if isinstance(x.get("calibration_target"), (int, float))]
    out_dataset = OUT_DIR / "score_calibration_dataset.jsonl"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with out_dataset.open("w", encoding="utf-8") as fh:
        for row in valid_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    train_days = max(1, min(int(args.train_days), len(days) - 1))
    train_set = set(days[:train_days])
    validate_set = set(days[train_days:])
    train_rows = [x for x in valid_rows if str(x.get("date")) in train_set]
    validate_rows = [x for x in valid_rows if str(x.get("date")) in validate_set]

    features = ["stability", "drawdown", "consistency", "frequency", "cost_sensitivity"]
    feature_corr: dict[str, float] = {}
    for feature in features:
        xs = [float((x.get("legacy_sub_scores") or {}).get(feature) or 0.0) for x in train_rows]
        ys = [float(x.get("calibration_target") or 0.0) for x in train_rows]
        feature_corr[feature] = _corr(xs, ys)

    strengths: dict[str, float] = {}
    direction: dict[str, str] = {}
    for feature, corr in feature_corr.items():
        if corr < -0.02:
            direction[feature] = "negative"
            strengths[feature] = abs(corr)
        elif corr <= 0.02:
            direction[feature] = "neutral"
            strengths[feature] = 0.001
        else:
            direction[feature] = "positive"
            strengths[feature] = corr

    strength_sum = sum(strengths.values()) or 1.0
    base_weights = {feature: float(strength / strength_sum) for feature, strength in strengths.items()}

    transform_candidates = ["identity", "square", "sqrt"]
    best_value = -1e9
    best_weights = base_weights.copy()
    best_transform = {feature: "identity" for feature in features}
    best_detail: dict[str, Any] = {}

    # weight-level only search: scale factors + simple monotonic transform
    scales = [0.7, 1.0, 1.3]
    for scale_stability in scales:
        for scale_drawdown in scales:
            for scale_consistency in scales:
                for scale_frequency in scales:
                    for scale_cost in scales:
                        scaled = {
                            "stability": base_weights["stability"] * scale_stability,
                            "drawdown": base_weights["drawdown"] * scale_drawdown,
                            "consistency": base_weights["consistency"] * scale_consistency,
                            "frequency": base_weights["frequency"] * scale_frequency,
                            "cost_sensitivity": base_weights["cost_sensitivity"] * scale_cost,
                        }
                        norm = sum(scaled.values()) or 1.0
                        weights = {k: v / norm for k, v in scaled.items()}

                        for transform_drawdown in transform_candidates:
                            for transform_frequency in transform_candidates:
                                transform_map = {
                                    "stability": "identity",
                                    "drawdown": transform_drawdown,
                                    "consistency": "identity",
                                    "frequency": transform_frequency,
                                    "cost_sensitivity": "identity",
                                }
                                working = [dict(row) for row in train_rows]
                                _apply_recal_score(working, weights, direction, transform_map)
                                value, detail = _objective_value(working, ranking_cfg)
                                if value > best_value:
                                    best_value = value
                                    best_weights = weights
                                    best_transform = transform_map
                                    best_detail = detail

    weights_recal = {feature: round(best_weights[feature], 6) for feature in features}

    recal_cfg = {
        "score_version": "recalibrated_score.v2_ranking",
        "created_at": datetime.now().astimezone().isoformat(),
        "based_on": str(TARGET_CFG.relative_to(ROOT)),
        "ranking_objective": str(RANK_OBJ_CFG.relative_to(ROOT)),
        "weights_legacy": legacy_cfg.get("weights") or {},
        "feature_correlations_train": {k: round(v, 6) for k, v in feature_corr.items()},
        "weights_recalibrated": weights_recal,
        "best_train_objective": {
            "value": round(best_value, 6),
            "detail": best_detail,
            "transform_map": best_transform,
        },
        "sub_score_mapping": {
            feature: {
                "source_sub_score": feature,
                "direction": direction[feature],
                "transform": best_transform.get(feature, "identity"),
                "note": (
                    "reversed_due_to_negative_correlation"
                    if direction[feature] == "negative"
                    else "downweighted_due_to_weak_correlation"
                    if direction[feature] == "neutral"
                    else "positive_alignment"
                ),
            }
            for feature in features
        },
    }
    _dump_json(RECAL_SCORE_CFG, recal_cfg)

    def evaluate(rows_subset: list[dict[str, Any]], score_field: str) -> dict[str, Any]:
        if not rows_subset:
            return {
                "correlation": 0.0,
                "groups": {"top1": 0.0, "top3": 0.0, "top5": 0.0, "unpublished": 0.0},
                "constraints": {
                    "top1_mean_gte_top5_mean": False,
                    "top3_mean_gte_top5_mean": False,
                    "top5_mean_gt_unpublished_mean": False,
                },
            }
        xs = [float((x.get(score_field) or {}).get("total") or 0.0) for x in rows_subset]
        ys = [float(x.get("calibration_target") or 0.0) for x in rows_subset]
        correlation = _corr(xs, ys)
        groups = _rank_group_stats(rows_subset, score_field)
        constraints = {
            "top1_mean_gte_top5_mean": bool(groups.get("top1", 0.0) >= groups.get("top5", 0.0)),
            "top3_mean_gte_top5_mean": bool(groups.get("top3", 0.0) >= groups.get("top5", 0.0)),
            "top5_mean_gt_unpublished_mean": bool(groups.get("top5", 0.0) > groups.get("unpublished", 0.0)),
        }
        return {"correlation": round(correlation, 6), "groups": groups, "constraints": constraints}

    _apply_recal_score(train_rows, best_weights, direction, best_transform)
    _apply_recal_score(validate_rows, best_weights, direction, best_transform)
    _apply_recal_score(valid_rows, best_weights, direction, best_transform)

    holdout = {
        "train_days": sorted(train_set),
        "validate_days": sorted(validate_set),
        "train": {
            "legacy": evaluate(train_rows, "legacy_score"),
            "recalibrated": evaluate(train_rows, "recalibrated_score"),
        },
        "validate": {
            "legacy": evaluate(validate_rows, "legacy_score"),
            "recalibrated": evaluate(validate_rows, "recalibrated_score"),
        },
    }

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "dataset_rows": len(valid_rows),
        "train_rows": len(train_rows),
        "validate_rows": len(validate_rows),
        "weights_legacy": legacy_cfg.get("weights") or {},
        "feature_correlations_train": {k: round(v, 6) for k, v in feature_corr.items()},
        "weights_recalibrated": weights_recal,
        "direction_map": direction,
        "transform_map": best_transform,
        "holdout_validation": holdout,
        "paths": {
            "dataset_jsonl": str(out_dataset),
            "recalibrated_score_config": str(RECAL_SCORE_CFG),
        },
    }

    _dump_json(OUT_DIR / "score_calibration_summary.json", summary)
    print(str(OUT_DIR / "score_calibration_summary.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
