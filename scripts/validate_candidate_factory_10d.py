#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "runtime" / "state" / "candidate_factory" / "validation_10d"
FACTORY = ROOT / "scripts" / "daily_candidate_factory.py"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _run_factory(one_day: str) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    proc = subprocess.run(
        [
            str(ROOT / "venv" / "bin" / "python"),
            str(FACTORY),
            "--date",
            one_day,
            "--top-n",
            "5",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    status = _load_json(ROOT / "runtime" / "state" / "candidate_factory" / "daily_candidate_factory_status.json")
    top = _load_json(ROOT / "runtime" / "state" / "candidate_factory" / "daily_top_candidates.json")
    return proc.returncode == 0 and bool(status.get("ok")), status, top


def _candidate_key(candidate: dict[str, Any]) -> str:
    return str(candidate.get("candidate_signature") or candidate.get("candidate_id") or "")


def _build_tracker(day_tops: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    tracker_rows: list[dict[str, Any]] = []
    day_list = sorted(day_tops.keys())

    for day in day_list:
        candidates = (day_tops[day].get("candidates") or [])
        for idx, candidate in enumerate(candidates, start=1):
            key = _candidate_key(candidate)

            def perf_at(offset: int) -> dict[str, Any] | None:
                base_idx = day_list.index(day)
                target_idx = base_idx + offset
                if target_idx >= len(day_list):
                    return None
                target_day = day_list[target_idx]
                target_rows = day_tops[target_day].get("candidates") or []
                for row in target_rows:
                    if _candidate_key(row) == key:
                        evidence = row.get("evidence_summary") or {}
                        backtest = evidence.get("backtest") or {}
                        return {
                            "day": target_day,
                            "net_pnl_usd": backtest.get("net_pnl_usd"),
                            "max_drawdown_pct": backtest.get("max_drawdown_pct"),
                            "score": (row.get("score") or {}).get("total"),
                        }
                return None

            row = {
                "candidate_id": candidate.get("candidate_id"),
                "candidate_signature": key,
                "publish_date": day,
                "rank": idx,
                "score": (candidate.get("score") or {}).get("total"),
                "family_type": {
                    "strategy_family": candidate.get("strategy_family"),
                    "candidate_type": candidate.get("candidate_type"),
                },
                "expected_environment": candidate.get("applicable_environment"),
                "publish_evidence_summary": candidate.get("evidence_summary"),
                "post_publish": {
                    "t_plus_1": perf_at(1),
                    "t_plus_3": perf_at(3),
                    "t_plus_5": perf_at(5),
                },
                "still_worth_review": True,
                "is_false_positive": False,
            }

            t1 = (row["post_publish"].get("t_plus_1") or {}).get("net_pnl_usd")
            t3 = (row["post_publish"].get("t_plus_3") or {}).get("net_pnl_usd")
            if isinstance(t1, (int, float)) and isinstance(t3, (int, float)) and t1 < 0 and t3 < 0:
                row["still_worth_review"] = False
                row["is_false_positive"] = True

            tracker_rows.append(row)

    tracker = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "rows": tracker_rows,
    }

    summary = {
        "total_rows": len(tracker_rows),
        "false_positive_count": sum(1 for x in tracker_rows if x.get("is_false_positive")),
        "still_worth_review_count": sum(1 for x in tracker_rows if x.get("still_worth_review")),
    }
    return tracker, summary


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _correlation(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = sum((x - mx) ** 2 for x in xs)
    den_y = sum((y - my) ** 2 for y in ys)
    if den_x <= 0 or den_y <= 0:
        return 0.0
    return num / ((den_x ** 0.5) * (den_y ** 0.5))


def _discrimination(day_tops: dict[str, dict[str, Any]], day_rejected: dict[str, dict[str, Any]], day_status: dict[str, dict[str, Any]]) -> dict[str, Any]:
    top1_scores: list[float] = []
    top5_avg_scores: list[float] = []
    top5_avg_drawdown: list[float] = []
    top5_avg_stability: list[float] = []
    unpublished_scores: list[float] = []
    all_score_perf_pairs: list[tuple[float, float]] = []

    for day in sorted(day_tops.keys()):
        rows = day_tops[day].get("candidates") or []
        if not rows:
            continue
        scores = [float((r.get("score") or {}).get("total") or 0.0) for r in rows]
        top1_scores.append(scores[0])
        top5_avg_scores.append(_mean(scores))

        drawdowns = [float((((r.get("evidence_summary") or {}).get("backtest") or {}).get("max_drawdown_pct") or 0.0)) for r in rows]
        stabilities = [float((((r.get("score") or {}).get("sub_scores") or {}).get("stability") or 0.0)) for r in rows]
        top5_avg_drawdown.append(_mean(drawdowns))
        top5_avg_stability.append(_mean(stabilities))

        published_ids = {str(r.get("candidate_id") or "") for r in rows}
        status = day_status.get(day, {})
        steps = status.get("steps") if isinstance(status.get("steps"), list) else []
        freshness_passed: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("name") == "freshness_gate":
                detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
                raw_rows = detail.get("passed") if isinstance(detail.get("passed"), list) else []
                freshness_passed = [x for x in raw_rows if isinstance(x, dict)]
                break

        for candidate in freshness_passed:
            cid = str(candidate.get("candidate_id") or "")
            if cid in published_ids:
                continue
            score = float(((candidate.get("score") or {}).get("total") or 0.0))
            unpublished_scores.append(score)

        rejected = day_rejected.get(day, {}).get("rejected") or []
        for row in rejected:
            if row.get("stage") not in {"freshness_gate", "blacklist_gate"}:
                continue
            if isinstance(row.get("current_score"), (int, float)):
                unpublished_scores.append(float(row.get("current_score")))

        for row in rows:
            score = float((row.get("score") or {}).get("total") or 0.0)
            perf = float((((row.get("evidence_summary") or {}).get("backtest") or {}).get("net_pnl_usd") or 0.0))
            all_score_perf_pairs.append((score, perf))

    avg_top1 = _mean(top1_scores)
    avg_top5 = _mean(top5_avg_scores)
    avg_unpub = _mean(unpublished_scores)
    avg_dd = _mean(top5_avg_drawdown)
    avg_stability = _mean(top5_avg_stability)

    corr = _correlation([x for x, _ in all_score_perf_pairs], [y for _, y in all_score_perf_pairs])

    if avg_top1 > avg_top5 and avg_top5 > avg_unpub and corr > 0.15:
        verdict = "有明显区分度"
    elif (avg_top1 > avg_top5 or avg_top5 > avg_unpub) and corr > 0.02:
        verdict = "有弱区分度"
    else:
        verdict = "几乎无区分度"

    return {
        "verdict": verdict,
        "numbers": {
            "avg_top1_score": round(avg_top1, 4),
            "avg_top5_score": round(avg_top5, 4),
            "avg_unpublished_score": round(avg_unpub, 4),
            "avg_top5_drawdown": round(avg_dd, 6),
            "avg_top5_stability": round(avg_stability, 6),
            "score_perf_correlation": round(corr, 6),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run 10-trading-day validation for candidate factory")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--end-date", default=date.today().isoformat())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    end_day = date.fromisoformat(str(args.end_date))
    trade_days = _recent_trading_days(count=max(1, int(args.days)), end_day=end_day)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_table: list[dict[str, Any]] = []
    day_tops: dict[str, dict[str, Any]] = {}
    day_rejected: dict[str, dict[str, Any]] = {}
    day_status: dict[str, dict[str, Any]] = {}

    for day in trade_days:
        ok, status, top = _run_factory(day)
        rejected = _load_json(ROOT / "runtime" / "state" / "candidate_factory" / "rejected_candidates.json")
        day_tops[day] = top
        day_rejected[day] = rejected
        day_status[day] = status
        summary = status.get("summary") or {}
        scores = [float((c.get("score") or {}).get("total") or 0.0) for c in (top.get("candidates") or [])]
        run_table.append(
            {
                "date": day,
                "ok": ok,
                "total_candidates": int(summary.get("total_candidates") or 0),
                "spec_passed": int(summary.get("spec_passed") or 0),
                "published": int(summary.get("published") or 0),
                "top_scores": [round(x, 4) for x in scores],
            }
        )

    tracker, tracker_summary = _build_tracker(day_tops)
    discrimination = _discrimination(day_tops, day_rejected, day_status)

    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "trade_days": trade_days,
        "all_success": all(bool(r.get("ok")) for r in run_table),
        "run_table": run_table,
        "tracker_summary": tracker_summary,
        "score_discrimination": discrimination,
    }

    (OUT_DIR / "candidate_quality_tracker.json").write_text(json.dumps(tracker, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "candidate_quality_summary.md").write_text(
        "\n".join(
            [
                "# Candidate Quality Summary (10D)",
                f"- All success: {report['all_success']}",
                f"- Discrimination verdict: {discrimination['verdict']}",
                f"- Key numbers: {json.dumps(discrimination['numbers'], ensure_ascii=False)}",
                f"- Tracker rows: {tracker_summary['total_rows']}",
                f"- False positives: {tracker_summary['false_positive_count']}",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "validation_10d_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(str(OUT_DIR / "validation_10d_report.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
