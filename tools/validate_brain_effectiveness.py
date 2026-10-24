#!/usr/bin/env python3
"""
Brain效果验证工具

验证AI Brain的ML管理建议是否真的有效。

验证方法:
1. 回溯分析: Brain建议 → 下一个job的结果
2. A/B对比: 有Brain建议的job vs 没有Brain建议的job
3. 参数相关性: window_days vs IC/AUC的关系
4. 趋势分析: Brain介入前后的成功率变化

Usage:
  python tools/validate_brain_effectiveness.py --days 7
  python tools/validate_brain_effectiveness.py --days 7 --output reports/brain_validation.json
"""

import argparse
import json
import math
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict
import sys

def _parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="分析最近N天的数据")
    ap.add_argument("--runs-dir", default="runs/ai_trader", help="runs目录路径")
    ap.add_argument("--output", help="输出JSON报告路径")
    ap.add_argument("--min-samples", type=int, default=5, help="最少样本数（低于此值报警）")
    return ap.parse_args()


def _load_events(days: int, runs_dir: Path) -> List[Dict[str, Any]]:
    """加载最近N天的ML events"""
    events = []
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days)

    for day_offset in range(days + 1):
        date = end_date - timedelta(days=day_offset)
        events_file = runs_dir / str(date) / "ml_events.jsonl"
        if not events_file.exists():
            continue

        with events_file.open() as f:
            for line in f:
                evt = json.loads(line)
                if evt.get('status') == 'completed' and evt.get('job_type') == 'oof':
                    try:
                        evt['_ts_parsed'] = datetime.fromisoformat(evt['ts'])
                        events.append(evt)
                    except Exception:
                        pass

    return sorted(events, key=lambda e: e['_ts_parsed'])


def _load_actions(days: int, runs_dir: Path) -> List[Dict[str, Any]]:
    """加载最近N天的Brain actions"""
    actions = []
    end_date = datetime.now().date()

    for day_offset in range(days + 1):
        date = end_date - timedelta(days=day_offset)
        actions_file = runs_dir / str(date) / "ml_actions.jsonl"
        if not actions_file.exists():
            continue

        with actions_file.open() as f:
            for line in f:
                act = json.loads(line)
                if act.get('executed_at'):
                    try:
                        act['_ts_parsed'] = datetime.fromisoformat(act['ts'])
                        act['_executed_at_parsed'] = datetime.fromisoformat(act['executed_at'])
                        actions.append(act)
                    except Exception:
                        pass

    return sorted(actions, key=lambda a: a['_executed_at_parsed'])


def _classify_job(evt: Dict[str, Any]) -> Dict[str, Any]:
    """分类job的结果（基于 OOF 事件的 metrics 进行粗略健康判断）。

    注意：
    - 早期版本中，ic 为 None 时直接归类为 data_corruption。
      随着管线演化，ic 为空更多意味着“未计算”或“无信号”，不应等同于数据损坏。
    """
    ok = evt.get('ok', False)
    metrics = evt.get('metrics') or {}
    ic = metrics.get('fusion_ic')
    auc = metrics.get('fusion_auc')
    n_bars = metrics.get('n_bars')

    # 显式错误信号（如果存在）
    explicit_err = evt.get('error') or metrics.get('error') or metrics.get('fatal_error')

    # 分类逻辑：
    # 1) ok=True → success（即使 metrics 为空）
    # 2) 有显式 error → job_failed
    # 3) metrics 为空或无 n_bars → no_data（无法评估，不再视为 data_corruption）
    # 4) n_bars>0 但 ic 缺失 → no_ic（数据存在，但未计算/无法计算 IC）
    # 5) 其余按 IC 数值细分
    if ok:
        quality = "success"
    elif explicit_err:
        quality = "job_failed"
    elif not metrics or n_bars in (None, 0):
        quality = "no_data"
    elif ic is None:
        quality = "no_ic"
    elif isinstance(ic, (int, float)) and (math.isnan(ic) or math.isinf(ic)):
        quality = "ic_nan"
    elif abs(float(ic)) < 0.001:
        quality = "no_signal"
    elif float(ic) < 0:
        quality = "inverse_signal"
    elif 0 < float(ic) <= 0.02:
        quality = "weak_signal"
    else:
        quality = "low_auc"

    return {
        'ok': ok,
        'quality': quality,
        'ic': ic,
        'auc': auc,
        'n_bars': n_bars,
        'ts': evt['_ts_parsed']
    }


def _match_actions_to_jobs(actions: List[Dict], events: List[Dict]) -> Dict[str, Any]:
    """匹配Brain动作到后续job"""
    matched = []

    for act in actions:
        act_time = act['_executed_at_parsed']

        # 找到动作后的下一个job
        next_jobs = [e for e in events if e['_ts_parsed'] > act_time]
        if not next_jobs:
            continue

        next_job = next_jobs[0]
        time_diff_min = (next_job['_ts_parsed'] - act_time).total_seconds() / 60

        job_result = _classify_job(next_job)

        matched.append({
            'action': act['action'],
            'params': act['params'],
            'triggered_by': act.get('job_id'),
            'executed_at': act_time,
            'next_job_time': next_job['_ts_parsed'],
            'time_diff_min': time_diff_min,
            'next_job_result': job_result
        })

    return matched


def _compute_baseline(events: List[Dict], actions: List[Dict]) -> Dict[str, Any]:
    """计算无Brain干预的基线指标"""
    # 找出没有被Brain动作影响的job（动作执行前30分钟以上的job）
    action_times = [a['_executed_at_parsed'] for a in actions]

    baseline_jobs = []
    for evt in events:
        job_time = evt['_ts_parsed']

        # 检查这个job是否在任何action的30分钟窗口内
        influenced = False
        for act_time in action_times:
            if act_time - timedelta(minutes=30) < job_time < act_time + timedelta(hours=6):
                influenced = True
                break

        if not influenced:
            baseline_jobs.append(_classify_job(evt))

    if not baseline_jobs:
        return {"n": 0, "success_rate": None, "avg_ic": None, "avg_auc": None}

    success_count = sum(1 for j in baseline_jobs if j['ok'])
    ics = [j['ic'] for j in baseline_jobs if j['ic'] is not None and isinstance(j['ic'], (int, float)) and not (abs(j['ic']) != j['ic'] or j['ic'] != j['ic'])]
    aucs = [j['auc'] for j in baseline_jobs if j['auc'] is not None]

    return {
        'n': len(baseline_jobs),
        'success_rate': success_count / len(baseline_jobs),
        'avg_ic': sum(ics) / len(ics) if ics else None,
        'avg_auc': sum(aucs) / len(aucs) if aucs else None,
        'quality_distribution': {
            q: sum(1 for j in baseline_jobs if j['quality'] == q)
            for q in set(j['quality'] for j in baseline_jobs)
        }
    }


def _analyze_window_correlation(events: List[Dict]) -> Dict[str, Any]:
    """分析window_days与IC/AUC的相关性"""
    data = []
    for evt in events:
        metrics = evt.get('metrics', {})
        n_bars = metrics.get('n_bars')
        ic = metrics.get('fusion_ic')
        auc = metrics.get('fusion_auc')

        if n_bars and ic is not None and auc is not None:
            if isinstance(ic, (int, float)) and not (abs(ic) != ic or ic != ic):  # 过滤NaN
                data.append({'n_bars': n_bars, 'ic': ic, 'auc': auc})

    if len(data) < 5:
        return {"correlation": None, "message": "Insufficient data"}

    # 简单分组统计
    small = [d for d in data if d['n_bars'] < 1000]
    medium = [d for d in data if 1000 <= d['n_bars'] < 20000]
    large = [d for d in data if d['n_bars'] >= 20000]

    def _avg(lst, key):
        vals = [d[key] for d in lst]
        return sum(vals) / len(vals) if vals else None

    return {
        'small_windows': {
            'n': len(small),
            'avg_ic': _avg(small, 'ic'),
            'avg_auc': _avg(small, 'auc')
        },
        'medium_windows': {
            'n': len(medium),
            'avg_ic': _avg(medium, 'ic'),
            'avg_auc': _avg(medium, 'auc')
        },
        'large_windows': {
            'n': len(large),
            'avg_ic': _avg(large, 'ic'),
            'avg_auc': _avg(large, 'auc')
        }
    }


def main():
    args = _parse_args()
    runs_dir = Path(args.runs_dir)

    print("="*70)
    print(f"Brain效果验证报告 (最近{args.days}天)")
    print("="*70)
    print()

    # 加载数据
    events = _load_events(args.days, runs_dir)
    actions = _load_actions(args.days, runs_dir)

    print(f"📊 数据概览:")
    print(f"  - OOF jobs: {len(events)}")
    print(f"  - Brain actions (executed): {len(actions)}")
    print()

    if len(events) < args.min_samples:
        print(f"⚠️  警告: 样本数不足 ({len(events)} < {args.min_samples})")
        print(f"   建议: 至少收集{args.min_samples}个job后再评估\n")

    # 分析1: Brain动作 → 结果
    print("="*70)
    print("1. Brain动作效果分析")
    print("="*70)

    if not actions:
        print("⚠️  Brain尚未执行任何动作\n")
        brain_success_rate = None
    else:
        matched = _match_actions_to_jobs(actions, events)

        if not matched:
            print("⚠️  Brain执行了动作，但后续没有job运行\n")
            brain_success_rate = None
        else:
            success_count = sum(1 for m in matched if m['next_job_result']['ok'])
            brain_success_rate = success_count / len(matched)

            print(f"\n✅ Brain影响的job: {len(matched)}")
            print(f"   成功率: {brain_success_rate:.1%} ({success_count}/{len(matched)})")

            # 显示每个动作
            for i, m in enumerate(matched, 1):
                result = m['next_job_result']
                status = "✅" if result['ok'] else "❌"
                print(f"\n   {i}. {m['action']}: {m['params']}")
                print(f"      等待时间: {m['time_diff_min']:.1f}分钟")
                print(f"      结果: {status} {result['quality']}")
                if result['ic'] is not None:
                    print(f"      IC={result['ic']:.4f}, AUC={result['auc']:.3f}")

    # 分析2: 基线对比
    print("\n" + "="*70)
    print("2. 基线对比 (无Brain干预的job)")
    print("="*70)

    baseline = _compute_baseline(events, actions)

    if baseline['n'] == 0:
        print("\n⚠️  所有job都受Brain影响，无基线数据")
        baseline_success_rate = None
    else:
        baseline_success_rate = baseline['success_rate']
        print(f"\n📊 基线job: {baseline['n']}")
        print(f"   成功率: {baseline_success_rate:.1%}")
        if baseline['avg_ic']:
            print(f"   平均IC: {baseline['avg_ic']:.4f}")
            print(f"   平均AUC: {baseline['avg_auc']:.3f}")

        print(f"\n   失败分布:")
        for quality, count in baseline['quality_distribution'].items():
            if quality != 'success':
                print(f"     - {quality}: {count}")

    # 分析3: 效果判断
    print("\n" + "="*70)
    print("3. 效果评估")
    print("="*70)
    print()

    if brain_success_rate is not None and baseline_success_rate is not None:
        improvement = brain_success_rate - baseline_success_rate

        if len(matched) < args.min_samples:
            print(f"⚠️  样本不足 (Brain: {len(matched)}, 需要: ≥{args.min_samples})")
            print(f"   当前数据: Brain {brain_success_rate:.1%} vs 基线 {baseline_success_rate:.1%}")
            print(f"   差异: {improvement:+.1%} (但统计不显著)\n")
            conclusion = "insufficient_data"
        elif improvement > 0.05:
            print(f"✅ Brain有效!")
            print(f"   Brain成功率: {brain_success_rate:.1%}")
            print(f"   基线成功率: {baseline_success_rate:.1%}")
            print(f"   提升: {improvement:+.1%}\n")
            conclusion = "effective"
        elif improvement > -0.05:
            print(f"⚠️  Brain效果不明显")
            print(f"   Brain: {brain_success_rate:.1%}, 基线: {baseline_success_rate:.1%}")
            print(f"   差异: {improvement:+.1%} (接近0)\n")
            conclusion = "neutral"
        else:
            print(f"❌ Brain可能有害!")
            print(f"   Brain: {brain_success_rate:.1%}, 基线: {baseline_success_rate:.1%}")
            print(f"   下降: {improvement:+.1%}\n")
            conclusion = "harmful"
    else:
        print("⚠️  无法评估 (缺少Brain或基线数据)\n")
        conclusion = "no_data"

    # 分析4: 窗口大小相关性
    print("="*70)
    print("4. 窗口大小与质量相关性")
    print("="*70)

    window_corr = _analyze_window_correlation(events)

    if window_corr.get('correlation') is None:
        print(f"\n⚠️  {window_corr.get('message', 'No data')}\n")
    else:
        print("\n📊 按窗口大小分组:")
        for size_name, size_key in [('小窗口 (<1k bars)', 'small_windows'),
                                      ('中窗口 (1k-20k)', 'medium_windows'),
                                      ('大窗口 (≥20k)', 'large_windows')]:
            data = window_corr[size_key]
            if data['n'] > 0:
                print(f"\n   {size_name}: {data['n']} jobs")
                if data['avg_ic']:
                    print(f"     平均IC: {data['avg_ic']:.4f}")
                    print(f"     平均AUC: {data['avg_auc']:.3f}")

    # 分析5: 建议
    print("\n" + "="*70)
    print("5. 建议")
    print("="*70)
    print()

    if conclusion == "insufficient_data":
        print("📋 继续收集数据:")
        print(f"   - 当前Brain样本: {len(matched) if actions else 0}")
        print(f"   - 需要样本: ≥{args.min_samples}")
        print(f"   - 建议: 再运行{args.min_samples - len(matched)}个job后重新评估")
    elif conclusion == "effective":
        print("✅ Brain表现良好，建议:")
        print("   1. 保持当前配置")
        print("   2. 可以降低actions_conf_floor至0.50以接受更多建议")
        print("   3. 考虑扩展Brain权限到Level 2 (阈值调优)")
    elif conclusion == "neutral":
        print("⚠️  Brain效果不明显，建议:")
        print("   1. 继续观察1-2天")
        print("   2. 检查Brain的诊断逻辑是否准确")
        print("   3. 可能需要调整Brain的建议策略")
    elif conclusion == "harmful":
        print("❌ Brain表现差，建议:")
        print("   1. 立即提高actions_conf_floor至0.70")
        print("   2. 审查Brain的诊断逻辑")
        print("   3. 考虑禁用某些动作类型")
    else:
        print("⚠️  数据不足，建议:")
        print("   1. 确保Brain已启用 (policies.ml.actions_conf_floor < 0.6)")
        print("   2. 运行更多OOF jobs")
        print("   3. 检查ml_events.jsonl和ml_actions.jsonl是否正常写入")

    # 输出JSON报告
    if args.output:
        report = {
            'analysis_date': datetime.now().isoformat(),
            'days_analyzed': args.days,
            'total_jobs': len(events),
            'brain_actions': len(actions),
            'brain_success_rate': brain_success_rate,
            'baseline_success_rate': baseline_success_rate,
            'conclusion': conclusion,
            'window_correlation': window_corr
        }

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2))
        print(f"\n📄 报告已保存: {args.output}")

    print("\n" + "="*70)


if __name__ == "__main__":
    main()
