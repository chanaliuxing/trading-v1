#!/usr/bin/env python3
"""
每日Brain OOF收敛报告

统计Brain vs 基线的成功率、IC/AUC提升、非回归拦截等指标
输出到 runs/ai_trader/<date>/brain_oof_report.json

Usage:
    python tools/daily_brain_report.py --date 2025-11-07
    python tools/daily_brain_report.py  # 默认今天
"""
import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict
import sys

def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """加载JSONL文件"""
    if not path.exists():
        return []

    records = []
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"[WARN] Skip invalid JSON line: {line[:100]}", file=sys.stderr)
    return records


def parse_timestamp(ts_str: str) -> Optional[datetime]:
    """解析时间戳字符串"""
    try:
        # 支持多种格式
        for fmt in ["%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"]:
            try:
                return datetime.strptime(ts_str, fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def match_actions_to_jobs(actions: List[Dict], events: List[Dict]) -> Dict[str, List[Dict]]:
    """将Brain动作匹配到后续job"""
    # 为每个job收集相关的Brain动作
    job_actions = defaultdict(list)

    for action in actions:
        if action.get("status") != "ok":
            continue

        job_id = action.get("job_id")
        action_ts = parse_timestamp(action.get("ts", ""))
        executed_at = parse_timestamp(action.get("executed_at", ""))

        if not executed_at:
            continue  # 未执行的动作不算

        # 找到这个动作之后的所有job
        for event in events:
            event_ts = parse_timestamp(event.get("ts", ""))
            if not event_ts:
                continue

            # 如果job在动作执行后6小时内启动，视为受影响
            if executed_at < event_ts < executed_at + timedelta(hours=6):
                job_actions[event.get("job_id", "")].append(action)

    return job_actions


def compute_metrics(events: List[Dict], job_actions: Dict[str, List[Dict]]) -> Dict[str, Any]:
    """计算Brain vs 基线的指标"""
    brain_jobs = []
    baseline_jobs = []

    for event in events:
        if event.get("status") != "completed":
            continue

        job_id = event.get("job_id", "")
        ok = event.get("ok", False)
        metrics = event.get("metrics", {})

        ic = metrics.get("fusion_ic")
        auc = metrics.get("fusion_auc")

        # 判断是否受Brain影响
        is_brain_influenced = job_id in job_actions and len(job_actions[job_id]) > 0

        job_record = {
            "job_id": job_id,
            "ok": ok,
            "ic": ic,
            "auc": auc,
        }

        if is_brain_influenced:
            brain_jobs.append(job_record)
        else:
            baseline_jobs.append(job_record)

    # 统计
    brain_success = sum(1 for j in brain_jobs if j["ok"])
    baseline_success = sum(1 for j in baseline_jobs if j["ok"])

    brain_rate = brain_success / len(brain_jobs) if brain_jobs else 0.0
    baseline_rate = baseline_success / len(baseline_jobs) if baseline_jobs else 0.0

    # IC/AUC平均（仅成功的）
    brain_ic_list = [j["ic"] for j in brain_jobs if j["ok"] and j["ic"] is not None and not isinstance(j["ic"], str)]
    baseline_ic_list = [j["ic"] for j in baseline_jobs if j["ok"] and j["ic"] is not None and not isinstance(j["ic"], str)]

    brain_auc_list = [j["auc"] for j in brain_jobs if j["ok"] and j["auc"] is not None and not isinstance(j["auc"], str)]
    baseline_auc_list = [j["auc"] for j in baseline_jobs if j["ok"] and j["auc"] is not None and not isinstance(j["auc"], str)]

    brain_ic = sum(brain_ic_list) / len(brain_ic_list) if brain_ic_list else 0.0
    baseline_ic = sum(baseline_ic_list) / len(baseline_ic_list) if baseline_ic_list else 0.0

    brain_auc = sum(brain_auc_list) / len(brain_auc_list) if brain_auc_list else 0.0
    baseline_auc = sum(baseline_auc_list) / len(baseline_auc_list) if baseline_auc_list else 0.0

    return {
        "brain": {
            "total": len(brain_jobs),
            "success": brain_success,
            "success_rate": brain_rate,
            "avg_ic": brain_ic,
            "avg_auc": brain_auc,
        },
        "baseline": {
            "total": len(baseline_jobs),
            "success": baseline_success,
            "success_rate": baseline_rate,
            "avg_ic": baseline_ic,
            "avg_auc": baseline_auc,
        },
        "lift": {
            "success_rate": brain_rate - baseline_rate,
            "ic": brain_ic - baseline_ic,
            "auc": brain_auc - baseline_auc,
        },
        "statistical_significance": len(brain_jobs) >= 10,  # N≥10才有统计意义
    }


def count_non_regression_blocks(events: List[Dict]) -> int:
    """统计非回归保护拦截次数"""
    # 查找事件中的非回归拦截标记
    block_count = 0
    for event in events:
        if event.get("status") == "failed":
            reason = event.get("reason", "")
            if "non_regression" in reason.lower() or "regression" in reason.lower():
                block_count += 1
    return block_count


def generate_report(date: str) -> Dict[str, Any]:
    """生成指定日期的报告"""
    runs_dir = Path("runs/ai_trader") / date

    events_file = runs_dir / "ml_events.jsonl"
    actions_file = runs_dir / "ml_actions.jsonl"

    if not events_file.exists():
        print(f"[ERROR] Events file not found: {events_file}", file=sys.stderr)
        return {"error": f"Events file not found: {events_file}"}

    # 加载数据
    events = load_jsonl(events_file)
    actions = load_jsonl(actions_file)

    print(f"[INFO] Loaded {len(events)} events, {len(actions)} actions")

    # 匹配动作到job
    job_actions = match_actions_to_jobs(actions, events)
    brain_influenced_count = len(job_actions)

    print(f"[INFO] Found {brain_influenced_count} Brain-influenced jobs")

    # 计算指标
    metrics = compute_metrics(events, job_actions)

    # 统计非回归拦截
    nr_blocks = count_non_regression_blocks(events)

    # 统计动作类型
    action_types = defaultdict(int)
    for action in actions:
        if action.get("status") == "ok":
            action_types[action.get("action", "unknown")] += 1

    # 汇总
    report = {
        "date": date,
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_events": len(events),
            "total_actions": len(actions),
            "brain_influenced_jobs": brain_influenced_count,
            "non_regression_blocks": nr_blocks,
        },
        "metrics": metrics,
        "action_types": dict(action_types),
    }

    return report


def main():
    parser = argparse.ArgumentParser(description="生成每日Brain OOF收敛报告")
    parser.add_argument("--date", type=str, default=None, help="日期 (YYYY-MM-DD)，默认今天")
    parser.add_argument("--output", type=str, default=None, help="输出文件路径，默认runs/ai_trader/<date>/brain_oof_report.json")
    args = parser.parse_args()

    # 确定日期
    if args.date:
        date = args.date
    else:
        date = datetime.now().strftime("%Y-%m-%d")

    print(f"[INFO] Generating report for {date}")

    # 生成报告
    report = generate_report(date)

    # 输出路径
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path("runs/ai_trader") / date / "brain_oof_report.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"[INFO] Report saved to {output_path}")

    # 打印摘要
    print("\n" + "="*60)
    print(f"Brain OOF Report - {date}")
    print("="*60)

    if "error" in report:
        print(f"ERROR: {report['error']}")
        return 1

    summary = report["summary"]
    metrics = report["metrics"]

    print(f"\n📊 Summary:")
    print(f"  Total Events: {summary['total_events']}")
    print(f"  Total Actions: {summary['total_actions']}")
    print(f"  Brain-influenced Jobs: {summary['brain_influenced_jobs']}")
    print(f"  Non-regression Blocks: {summary['non_regression_blocks']}")

    print(f"\n🤖 Brain Performance:")
    print(f"  Jobs: {metrics['brain']['total']}")
    print(f"  Success: {metrics['brain']['success']} ({metrics['brain']['success_rate']*100:.1f}%)")
    print(f"  Avg IC: {metrics['brain']['avg_ic']:.4f}")
    print(f"  Avg AUC: {metrics['brain']['avg_auc']:.4f}")

    print(f"\n📈 Baseline Performance:")
    print(f"  Jobs: {metrics['baseline']['total']}")
    print(f"  Success: {metrics['baseline']['success']} ({metrics['baseline']['success_rate']*100:.1f}%)")
    print(f"  Avg IC: {metrics['baseline']['avg_ic']:.4f}")
    print(f"  Avg AUC: {metrics['baseline']['avg_auc']:.4f}")

    print(f"\n🎯 Lift:")
    print(f"  Success Rate: {metrics['lift']['success_rate']*100:+.1f}%")
    print(f"  IC: {metrics['lift']['ic']:+.4f}")
    print(f"  AUC: {metrics['lift']['auc']:+.4f}")

    if metrics['statistical_significance']:
        print(f"\n✅ Statistical Significance: YES (N={metrics['brain']['total']}≥10)")
    else:
        print(f"\n⚠️  Statistical Significance: NO (N={metrics['brain']['total']}<10)")

    print(f"\n🔧 Action Types:")
    for action_type, count in report["action_types"].items():
        print(f"  {action_type}: {count}")

    print("\n" + "="*60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
