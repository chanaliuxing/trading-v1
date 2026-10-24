#!/usr/bin/env python3
from __future__ import annotations

"""
Brain Dashboard
---------------

一键汇总 AI 大脑的核心健康信号：

1. 昨天 vs 上一次 ML job 的 Sharpe / IC 变化（brain_daily_report）
2. 最近 N 天 Brain 动作的整体效果（validate_brain_effectiveness）
3. 简单文件存在性检查，确认基础日志是否正常写入

Usage:
    python tools/brain_dashboard.py
    python tools/brain_dashboard.py --days 7 --runs-dir runs/ai_trader
"""

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Tuple


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="AI Brain one-shot dashboard")
    ap.add_argument(
        "--days",
        type=int,
        default=7,
        help="最近 N 天的窗口（传给 validate_brain_effectiveness，默认 7）",
    )
    ap.add_argument(
        "--runs-dir",
        default="runs/ai_trader",
        help="runs 根目录（默认 runs/ai_trader）",
    )
    ap.add_argument(
        "--python-bin",
        default=sys.executable,
        help="Python 解释器路径（默认当前进程的 sys.executable）",
    )
    return ap.parse_args()


def _run_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    """Run a subprocess and capture stdout/stderr as text."""
    proc = subprocess.run(cmd, text=True, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def _print_section(title: str) -> None:
    bar = "=" * 70
    print()
    print(bar)
    print(title)
    print(bar)


def _check_core_files(runs_dir: Path) -> None:
    """Lightweight sanity check: today/yesterday key files exist."""
    _print_section("0. 基础文件自检")
    today = date.today()
    yesterday = today.fromordinal(today.toordinal() - 1)
    for d in (yesterday, today):
        day_dir = runs_dir / d.isoformat()
        ml_events = day_dir / "ml_events.jsonl"
        ml_actions = day_dir / "ml_actions.jsonl"
        status = []
        status.append(f"ml_events={'OK' if ml_events.exists() else 'MISSING'}")
        status.append(f"ml_actions={'OK' if ml_actions.exists() else 'MISSING'}")
        print(f"- {d.isoformat()}: {', '.join(status)}")


def _summarize_experiments(runs_dir: Path) -> None:
    """Summarize today's OOF experiments (if any) for quick inspection."""
    _print_section("3. OOF 实验状态 (label/horizon 探索)")
    dstr = date.today().isoformat()
    exp_file = runs_dir / dstr / "ml_experiments.jsonl"
    if not exp_file.exists():
        print(f"⚠️  {dstr} 无 ml_experiments.jsonl，可能尚未有 OOF 实验记录或 control_consumer 未运行。")
        return

    try:
        total = 0
        positive = 0
        auc_vals: List[float] = []
        ic_vals: List[float] = []
        with exp_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("job_type") or "").lower() != "oof":
                    continue
                metrics = row.get("metrics") or {}
                auc = metrics.get("fusion_auc", metrics.get("auc"))
                ic = metrics.get("fusion_ic", metrics.get("ic"))
                if not isinstance(auc, (int, float)) or not isinstance(ic, (int, float)):
                    continue
                auc_f = float(auc)
                ic_f = float(ic)
                total += 1
                auc_vals.append(auc_f)
                ic_vals.append(ic_f)
                if (auc_f >= 0.52) and (abs(ic_f) >= 0.02):
                    positive += 1

        if total == 0:
            print(f"⚠️  {dstr} 找不到任何 OOF 实验记录（ml_experiments.jsonl 中无 job_type='oof' 行）。")
            return

        mean_auc = sum(auc_vals) / len(auc_vals) if auc_vals else None
        mean_ic = sum(ic_vals) / len(ic_vals) if ic_vals else None
        no_edge_confirmed = bool(total >= 10 and positive == 0)

        print(f"- 日期: {dstr}")
        print(f"- 实验总数: {total}")
        print(f"- 有正向 edge 的实验数 (AUC>=0.52 且 |IC|>=0.02): {positive}")
        if mean_auc is not None:
            print(f"- 平均 AUC: {mean_auc:.3f}")
        if mean_ic is not None:
            print(f"- 平均 IC: {mean_ic:.4f}")
        print(f"- no_edge_confirmed: {no_edge_confirmed}")

        if no_edge_confirmed:
            print("\n⚠️  提示: 在当前 label/horizon 组合下，所有实验都未通过基本 edge 门槛。")
            print("   建议：")
            print("   - 让大脑尝试改变 label_horizon / 频率 / 特征家族；")
            print("   - 或在 Brain review 中接受 pause_ml / 改任务设定的建议。")
    except Exception as e:
        print(f"⚠️  读取或解析 {exp_file} 失败: {e}")


def _summarize_ch_experiments(days: int) -> None:
    """Optional: summarize experiments from ClickHouse llm.ai_ml_experiments if available."""
    _print_section(f"4. ClickHouse 实验表摘要（最近 {days} 天）")
    try:
        import clickhouse_connect  # type: ignore
    except Exception as e:
        print(f"⚠️  clickhouse_connect 不可用或未安装: {e}")
        return

    # Reuse ai_trader ClickHouse DSN if present
    cfg_path = Path("scripts/ai_trader/config/ai_trader.yaml")
    host = "localhost"
    port = 8123
    user = "default"
    password = ""
    database = "llm"
    try:
        import yaml  # type: ignore

        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text("utf-8")) or {}
            ch_cfg = (cfg.get("clickhouse") or {})
            dsn = (ch_cfg.get("dsn") or "").strip()
            toks = dsn.split()
            for i, t in enumerate(toks):
                if t == "--host" and i + 1 < len(toks):
                    host = toks[i + 1]
                if t == "--port" and i + 1 < len(toks):
                    port = int(toks[i + 1])
                if t == "--user" and i + 1 < len(toks):
                    user = toks[i + 1]
                if t == "--password" and i + 1 < len(toks):
                    password = toks[i + 1]
                if t == "--database" and i + 1 < len(toks):
                    database = toks[i + 1]
    except Exception:
        pass

    try:
        cli = clickhouse_connect.get_client(
            host=host, port=port, username=user, password=password, database=database
        )
    except Exception as e:
        print(f"⚠️  无法连接 ClickHouse ({host}:{port}, db={database}): {e}")
        return

    # Query top label/horizon combos
    try:
        q = f"""
        SELECT
          window_days,
          horizon_min,
          label_bps,
          count() AS n,
          avg(fusion_auc) AS mean_auc,
          avg(fusion_ic) AS mean_ic
        FROM ai_ml_experiments
        WHERE ts >= now() - INTERVAL {int(days)} DAY
        GROUP BY window_days, horizon_min, label_bps
        ORDER BY mean_auc DESC, mean_ic DESC
        LIMIT 10
        """
        rows = cli.query(q).result_rows
    except Exception as e:
        print(f"⚠️  查询 ai_ml_experiments 失败: {e}")
        return

    if not rows:
        print("⚠️  最近没有任何实验行写入 ai_ml_experiments。")
        return

    print("Top 10 组合 (按 mean_auc, mean_ic 排序):")
    for window_days, horizon_min, label_bps, n, mean_auc, mean_ic in rows:
        print(
            f"  - window={window_days}d, horizon={horizon_min}m, label_bps={label_bps}: "
            f"n={n}, mean_auc={mean_auc:.3f}, mean_ic={mean_ic:.4f}"
        )


def main() -> None:
    args = _parse_args()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"AI Brain Dashboard Report - Generated at: {now_str}")
    runs_dir = Path(args.runs_dir)

    # 0. 基础文件存在性检查
    _check_core_files(runs_dir)

    # 1. Daily Brain Report（昨天 vs 上一次 job）
    _print_section("1. Daily Brain Report（昨天 vs 上一次 ML job）")
    rc, out, err = _run_cmd(
        [
            args.python_bin,
            "tools/brain_daily_report.py",
            "--days",
            "2",
            "--runs-dir",
            str(runs_dir),
        ]
    )
    if rc == 0:
        print(out.strip())
    else:
        print(f"[dashboard] brain_daily_report 失败 (rc={rc})")
        if err:
            print(err.strip())

    # 2. Brain 效果验证（最近 N 天）
    _print_section(f"2. Brain 效果验证（最近 {args.days} 天）")
    rc, out, err = _run_cmd(
        [
            args.python_bin,
            "tools/validate_brain_effectiveness.py",
            "--days",
            str(args.days),
            "--runs-dir",
            str(runs_dir),
        ]
    )
    if rc == 0:
        print(out.strip())
    else:
        print(f"[dashboard] validate_brain_effectiveness 失败 (rc={rc})")
        if err:
            print(err.strip())

    # 3. 实验状态（OOF 实验网格探索概要）
    _summarize_experiments(runs_dir)

    # 4. ClickHouse 实验表摘要（如果存在）
    _summarize_ch_experiments(args.days)


if __name__ == "__main__":
    main()
