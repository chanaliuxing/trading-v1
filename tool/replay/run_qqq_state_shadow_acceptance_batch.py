#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.report.build_qqq_state_shadow_batch import build_qqq_state_shadow_batch
from tool.report.build_qqq_state_shadow_cutover_recommendation import build_qqq_state_shadow_cutover_recommendation
from tool.report.build_qqq_state_shadow_rollup import build_qqq_state_shadow_rollup
from tool.replay.run_qqq_state_shadow_acceptance import run_qqq_state_shadow_acceptance

DEFAULT_QQQ_OPTION_STRATEGY_ID = "qqq_intraday_option_v1_001"
DEFAULT_QQQ_OPTION_BASELINE_PATH = ROOT / "docs" / "baselines" / "qqq_intraday_option_v1_001_replica_baseline.json"
LEGACY_QQQ_BASELINE_PATH = ROOT / "docs" / "baselines" / "qqq_intraday_001_replica_baseline.json"


def _default_baseline_path() -> Path:
    if DEFAULT_QQQ_OPTION_BASELINE_PATH.exists():
        return DEFAULT_QQQ_OPTION_BASELINE_PATH
    return LEGACY_QQQ_BASELINE_PATH


def _discover_clickhouse_days(
    *,
    state_instrumentation_start_date: str | None,
    max_days: int | None,
    strategy_id: str,
    clickhouse_table: str | None,
) -> list[str]:
    from libs.db.ch_pool import get_client
    from tool.query_audit import _discover_clickhouse_audit_tables

    client = get_client()
    tables = _discover_clickhouse_audit_tables(client, explicit_table=clickhouse_table)
    start_date = str(state_instrumentation_start_date or "").strip() or None
    all_days: set[str] = set()
    for table in tables:
        try:
            sql = """
            SELECT toDate(toTimeZone(ts, 'America/New_York')) AS d
            FROM {table_name}
            WHERE strategy_id = {strategy_id:String}
              AND domain IN ('strategy', 'state')
            GROUP BY d
            ORDER BY d
            """
            sql = sql.replace("{table_name}", table)
            rows = client.query(sql, parameters={"strategy_id": strategy_id})
            for row in rows:
                d = str(row.get("d") or "").strip()
                if len(d) != 10:
                    continue
                if start_date and d < start_date:
                    continue
                all_days.add(d)
        except Exception:
            continue
    out = sorted(all_days)
    if max_days is not None and int(max_days) > 0:
        out = out[-int(max_days) :]
    return out


def discover_available_days(
    *,
    audit_root: Path,
    state_instrumentation_start_date: str | None = "2026-04-03",
    max_days: int | None = None,
    source: str = "local",
    strategy_id: str = DEFAULT_QQQ_OPTION_STRATEGY_ID,
    clickhouse_table: str | None = None,
) -> list[str]:
    source_norm = str(source or "local").strip().lower()
    if source_norm in {"clickhouse", "ch"}:
        return _discover_clickhouse_days(
            state_instrumentation_start_date=state_instrumentation_start_date,
            max_days=max_days,
            strategy_id=strategy_id,
            clickhouse_table=clickhouse_table,
        )
    base = Path(audit_root).expanduser()
    if not base.exists():
        return []
    strategy_id_norm = str(strategy_id or "").strip()

    def _day_has_strategy_records(day_dir: Path) -> bool:
        for pattern in ("strategy-*.jsonl", "state-*.jsonl"):
            for path in day_dir.glob(pattern):
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if str(row.get("strategy_id") or "").strip() == strategy_id_norm:
                            return True
                except Exception:
                    continue
        return False

    start_date = str(state_instrumentation_start_date or "").strip() or None
    days: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        day = child.name.strip()
        if len(day) != 10:
            continue
        if start_date and day < start_date:
            continue
        if strategy_id_norm and not _day_has_strategy_records(child):
            continue
        days.append(day)
    if max_days is not None and int(max_days) > 0:
        days = days[-int(max_days) :]
    return days


def run_qqq_state_shadow_acceptance_batch(
    *,
    audit_root: Path,
    output_root: Path,
    days: list[str],
    strategy_id: str = DEFAULT_QQQ_OPTION_STRATEGY_ID,
    baseline_path: Path,
    state_instrumentation_start_date: str | None = "2026-04-03",
    source: str = "local",
    clickhouse_table: str | None = None,
) -> Dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    daily_reports: list[str] = []
    for day in list(days or []):
        daily = run_qqq_state_shadow_acceptance(
            audit_root=audit_root,
            output_root=output_root,
            date=str(day),
            strategy_id=strategy_id,
            baseline_path=baseline_path,
            state_instrumentation_start_date=state_instrumentation_start_date,
            source=source,
            clickhouse_table=clickhouse_table,
        )
        daily_reports.append(str(daily.get("report_path") or ""))
    batch = build_qqq_state_shadow_batch(
        audit_root=audit_root,
        output_root=output_root,
        days=days,
        strategy_id=strategy_id,
        baseline_path=baseline_path,
        state_instrumentation_start_date=state_instrumentation_start_date,
        source=source,
        clickhouse_table=clickhouse_table,
    )
    recommendation = build_qqq_state_shadow_cutover_recommendation(
        output_root=output_root,
        state_instrumentation_start_date=state_instrumentation_start_date,
    )
    rollup = build_qqq_state_shadow_rollup(output_root=output_root)
    report = {
        "strategy_id": strategy_id,
        "days": list(days),
        "daily_report_paths": daily_reports,
        "batch_report_path": str(batch.get("report_path") or ""),
        "cutover_recommendation_path": str(recommendation.get("report_path") or ""),
        "rollup_report_path": str(rollup.get("report_path") or ""),
        "all_days_pass": bool(batch.get("all_days_pass", False)),
        "recommendation": recommendation.get("recommendation"),
        "cutover_still_blocked": bool(recommendation.get("cutover_still_blocked", True)),
    }
    report_path = output_root / "qqq_state_shadow_acceptance_batch_pipeline.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run multi-day QQQ state shadow acceptance batch pipeline")
    parser.add_argument("--root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-root", default=str(ROOT / "runtime" / "state" / "qqq_state_shadow_acceptance"))
    parser.add_argument("--strategy-id", default=DEFAULT_QQQ_OPTION_STRATEGY_ID)
    parser.add_argument("--baseline-path", default=str(_default_baseline_path()))
    parser.add_argument("--state-instrumentation-start-date", default="2026-04-03")
    parser.add_argument("--source", choices=["local", "clickhouse", "auto"], default="local")
    parser.add_argument("--clickhouse-table")
    parser.add_argument("--days", nargs="+")
    parser.add_argument("--auto-days", action="store_true")
    parser.add_argument("--max-days", type=int)
    args = parser.parse_args()
    selected_days: list[str] = [str(item) for item in list(args.days or [])]
    start_date = str(args.state_instrumentation_start_date or "").strip() or None
    if bool(args.auto_days):
        selected_days = discover_available_days(
            audit_root=Path(args.root).expanduser(),
            state_instrumentation_start_date=start_date,
            max_days=args.max_days,
            source=str(args.source),
            strategy_id=str(args.strategy_id),
            clickhouse_table=str(args.clickhouse_table or "").strip() or None,
        )
    if not selected_days:
        empty_payload = run_qqq_state_shadow_acceptance_batch(
            audit_root=Path(args.root).expanduser(),
            output_root=Path(args.output_root).expanduser(),
            days=[],
            strategy_id=str(args.strategy_id),
            baseline_path=Path(args.baseline_path).expanduser(),
            state_instrumentation_start_date=start_date,
            source=str(args.source),
            clickhouse_table=str(args.clickhouse_table or "").strip() or None,
        )
        reason = "no_days_selected"
        source_norm = str(args.source or "").strip().lower()
        if source_norm in {"clickhouse", "ch"}:
            reason = "no_days_selected_clickhouse_state_audit_unavailable"
        empty_payload["recommendation"] = "BLOCK"
        empty_payload["cutover_still_blocked"] = True
        empty_payload["reason"] = reason
        empty_payload["auto_days"] = bool(args.auto_days)
        empty_payload["source"] = str(args.source)
        empty_payload["strategy_id"] = str(args.strategy_id)
        empty_payload["state_instrumentation_start_date"] = start_date
        empty_payload["hint"] = "QQQ state shadow acceptance needs strategy/state audit records, not market-only bars."
        print(json.dumps(empty_payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    payload = run_qqq_state_shadow_acceptance_batch(
        audit_root=Path(args.root).expanduser(),
        output_root=Path(args.output_root).expanduser(),
        days=selected_days,
        strategy_id=str(args.strategy_id),
        baseline_path=Path(args.baseline_path).expanduser(),
        state_instrumentation_start_date=start_date,
        source=str(args.source),
        clickhouse_table=str(args.clickhouse_table or "").strip() or None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not bool(payload.get("cutover_still_blocked", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
