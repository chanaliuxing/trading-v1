#!/usr/bin/env python3
"""
Brain AB对照验证脚本

按照3天验证计划触发测试job：
- Brain路径：window_days自主调整（由Brain通过hints控制）
- 基线路径：固定window_days=30

每天3轮（上午/下午/夜间），符号集TSLA/AAPL

Usage:
    # Day 1 - 全天3轮
    python tools/brain_ab_validation.py --day 1 --round morning
    python tools/brain_ab_validation.py --day 1 --round afternoon
    python tools/brain_ab_validation.py --day 1 --round night

    # Day 2-3 同理
    python tools/brain_ab_validation.py --day 2 --round morning
    python tools/brain_ab_validation.py --day 3 --round morning

    # 自动运行全天3轮
    python tools/brain_ab_validation.py --day 1 --all-rounds
"""
import argparse
import subprocess
import json
from pathlib import Path
from datetime import datetime, timedelta
import time
import sys


class BrainABValidator:
    """Brain AB验证器"""

    def __init__(self, day: int, symbols: list = None):
        self.day = day
        self.symbols = symbols or ["TSLA", "AAPL"]

        # 验证计划配置
        self.config = {
            1: {  # Day 1: 小窗口（预期成功，建立baseline）
                "brain": {"window_days": 7, "expect_brain_action": "extend_window"},
                "baseline": {"window_days": 30},
            },
            2: {  # Day 2: 中窗口（Brain可能调整）
                "brain": {"window_days": 30, "expect_brain_action": None},
                "baseline": {"window_days": 30},
            },
            3: {  # Day 3: 大窗口（Brain可能缩短）
                "brain": {"window_days": 60, "expect_brain_action": "reduce_window"},
                "baseline": {"window_days": 30},
            },
        }

        # 时间窗口配置
        self.time_windows = {
            "morning": ("09:30:00", "12:00:00"),   # 上午
            "afternoon": ("12:00:00", "16:00:00"),  # 下午
            "night": ("16:00:00", "09:30:00"),      # 夜间（跨天）
        }

    def generate_date_range(self, round_name: str) -> tuple:
        """生成时间范围"""
        # 默认使用最近30天数据
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=30)

        return (
            f"{start_date} 09:30:00",
            f"{end_date} 16:00:00"
        )

    def trigger_job(self, path: str, window_days: int, symbols: list, start: str, end: str) -> str:
        """触发OOF job"""
        symbols_str = ",".join(symbols)

        cmd = [
            "python", "tools/nightly_oof_flow.py",
            "--symbols", symbols_str,
            "--start", start,
            "--end", end,
            "--window-days", str(window_days),
            "--quick",
            "--max-iters", "2",
        ]

        print(f"\n[{path.upper()}] Triggering job...")
        print(f"  Command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1小时超时
            )

            if result.returncode == 0:
                print(f"  ✅ Job completed successfully")
                # 尝试从输出中提取job_id
                for line in result.stdout.split('\n'):
                    if 'job_id' in line.lower() or 'started' in line.lower():
                        print(f"  {line}")
                return "success"
            else:
                print(f"  ❌ Job failed with code {result.returncode}")
                print(f"  stderr: {result.stderr[:500]}")
                return "failed"

        except subprocess.TimeoutExpired:
            print(f"  ⏱️  Job timed out after 1 hour")
            return "timeout"
        except Exception as e:
            print(f"  ❌ Exception: {e}")
            return "error"

    def run_round(self, round_name: str):
        """运行一轮AB对照"""
        print("="*70)
        print(f"Brain AB Validation - Day {self.day} - Round {round_name.upper()}")
        print("="*70)
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Symbols: {', '.join(self.symbols)}")
        print()

        day_config = self.config.get(self.day)
        if not day_config:
            print(f"[ERROR] Invalid day: {self.day}")
            return

        start, end = self.generate_date_range(round_name)
        print(f"Date Range: {start} → {end}")
        print()

        # Brain路径
        brain_config = day_config["brain"]
        print(f"🤖 [BRAIN PATH]")
        print(f"   Window: {brain_config['window_days']} days")
        if brain_config.get("expect_brain_action"):
            print(f"   Expected Action: {brain_config['expect_brain_action']}")
        print()

        brain_result = self.trigger_job(
            path="brain",
            window_days=brain_config["window_days"],
            symbols=self.symbols,
            start=start,
            end=end,
        )

        # 等待5分钟，避免资源冲突
        print("\n⏳ Waiting 5 minutes before baseline job...")
        time.sleep(300)

        # 基线路径
        baseline_config = day_config["baseline"]
        print(f"\n📈 [BASELINE PATH]")
        print(f"   Window: {baseline_config['window_days']} days (fixed)")
        print()

        baseline_result = self.trigger_job(
            path="baseline",
            window_days=baseline_config["window_days"],
            symbols=self.symbols,
            start=start,
            end=end,
        )

        # 汇总
        print("\n" + "="*70)
        print("Round Summary")
        print("="*70)
        print(f"Brain: {brain_result}")
        print(f"Baseline: {baseline_result}")
        print()

        # 保存结果
        self.save_round_result(round_name, brain_result, baseline_result)

    def save_round_result(self, round_name: str, brain_result: str, baseline_result: str):
        """保存轮次结果"""
        today = datetime.now().strftime("%Y-%m-%d")
        output_dir = Path("runs/ai_trader") / today
        output_dir.mkdir(parents=True, exist_ok=True)

        result_file = output_dir / "brain_ab_results.jsonl"

        record = {
            "ts": datetime.now().isoformat(),
            "day": self.day,
            "round": round_name,
            "brain_result": brain_result,
            "baseline_result": baseline_result,
        }

        with open(result_file, 'a') as f:
            f.write(json.dumps(record) + '\n')

        print(f"[INFO] Result saved to {result_file}")

    def run_all_rounds(self):
        """运行全天3轮"""
        for round_name in ["morning", "afternoon", "night"]:
            self.run_round(round_name)

            # 如果不是最后一轮，等待1小时
            if round_name != "night":
                print(f"\n⏳ Waiting 1 hour before next round...")
                time.sleep(3600)


def main():
    parser = argparse.ArgumentParser(description="Brain AB对照验证")
    parser.add_argument("--day", type=int, required=True, choices=[1, 2, 3], help="验证天数（1-3）")
    parser.add_argument("--round", type=str, choices=["morning", "afternoon", "night"], help="轮次")
    parser.add_argument("--all-rounds", action="store_true", help="运行全天3轮")
    parser.add_argument("--symbols", type=str, default="TSLA,AAPL", help="符号集（逗号分隔）")
    args = parser.parse_args()

    if not args.round and not args.all_rounds:
        parser.error("Must specify either --round or --all-rounds")

    symbols = args.symbols.split(",")
    validator = BrainABValidator(day=args.day, symbols=symbols)

    if args.all_rounds:
        validator.run_all_rounds()
    else:
        validator.run_round(args.round)


if __name__ == "__main__":
    main()
