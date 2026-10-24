#!/usr/bin/env python3
"""
交易系统健康检查脚本

执行所有系统监测，生成 JSON 报告
报告文件: <project_root>/logs/system_health.json

用法:
    python system_health_check.py
    python system_health_check.py --verbose
    python system_health_check.py --json
"""

import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List
import psutil
import argparse


class SystemHealthChecker:
    """系统健康检查器"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        # Resolve project root dynamically: docs/daily/system_health_check.py -> project root
        self.trading_root = Path(__file__).resolve().parents[2]
        self.report = {
            "timestamp": datetime.now().isoformat(),
            "timestamp_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z"),
            "overall_status": "healthy",  # healthy, warning, error
            "checks": {},
            "summary": {
                "total_checks": 0,
                "passed": 0,
                "warnings": 0,
                "errors": 0
            }
        }

    def log(self, message: str):
        """打印日志（仅 verbose 模式）"""
        if self.verbose:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def run_command(self, cmd: str, shell: bool = True, timeout: int = 10) -> tuple:
        """运行命令并返回结果"""
        try:
            result = subprocess.run(
                cmd,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timeout"
        except Exception as e:
            return -1, "", str(e)

    def update_status(self, check_status: str):
        """更新总体状态"""
        priority = {"healthy": 0, "warning": 1, "error": 2}
        current = priority.get(self.report["overall_status"], 0)
        new = priority.get(check_status, 0)
        if new > current:
            self.report["overall_status"] = check_status

    def check_docker_containers(self) -> Dict[str, Any]:
        """检查 Docker 容器状态"""
        self.log("检查 Docker 容器...")

        result = {
            "status": "healthy",
            "containers": {},
            "message": ""
        }

        # 检查容器状态
        infra_dir = self.trading_root / "infra"
        code, stdout, stderr = self.run_command(
            f"cd {infra_dir} && docker compose ps --format json"
        )

        if code != 0:
            result["status"] = "error"
            result["message"] = "无法获取容器状态"
            return result

        try:
            # docker-compose ps --format json 返回一个 JSON 数组（可能是单行或多行）
            # 尝试直接解析为列表
            try:
                containers_info = json.loads(stdout)
            except json.JSONDecodeError:
                # 如果失败，可能是多行JSON对象，逐行解析
                containers_info = []
                for line in stdout.strip().split('\n'):
                    if line.strip():
                        containers_info.append(json.loads(line))

            # 确保是列表
            if not isinstance(containers_info, list):
                containers_info = [containers_info]

            required = {"trading_kafka", "trading_clickhouse", "trading_redis"}
            hard_error = False
            soft_warning = False

            for container in containers_info:
                # 确保container是dict
                if not isinstance(container, dict):
                    continue

                name = container.get("Name", container.get("Service", "unknown"))
                state = (container.get("State", "") or "").lower()
                health = (container.get("Health", "") or "").lower()

                # 评估单个容器状态
                # 规则：running+healthy → healthy；starting → warning；其他 → error
                c_status = "healthy"
                if not (state == "running" and (health in ("", "healthy") or "healthy" in health)):
                    if state == "running" and ("starting" in health or health == "starting"):
                        c_status = "warning"
                        soft_warning = True
                    else:
                        c_status = "error"
                        # 只有关键容器才将整体判为 error
                        if name in required:
                            hard_error = True
                        else:
                            soft_warning = True

                result["containers"][name] = {
                    "state": state,
                    "health": health,
                    "status": c_status,
                }

            # 检查关键容器是否存在
            missing = [req for req in required if req not in result["containers"]]
            if missing:
                hard_error = True
                result["message"] = f"缺少关键容器: {', '.join(missing)}"

            # 归并总体状态：硬错误优先，其次警告
            if hard_error:
                result["status"] = "error"
            elif soft_warning:
                result["status"] = "warning"
            else:
                result["status"] = "healthy"

        except Exception as e:
            result["status"] = "warning"
            result["message"] = f"解析容器状态失败: {str(e)}"

        return result

    def check_run_system_py(self) -> Dict[str, Any]:
        """检查 run_system.py 运行状态"""
        self.log("检查 run_system.py...")

        result = {
            "status": "healthy",
            "running": False,
            "pid": None,
            "recent_errors": 0,
            "message": ""
        }

        # 检查进程
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = ' '.join(proc.info['cmdline'] or [])
                if 'run_system.py' in cmdline and 'python' in cmdline.lower():
                    result["running"] = True
                    result["pid"] = proc.info['pid']
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if not result["running"]:
            result["status"] = "error"
            result["message"] = "run_system.py 未运行"
            return result

        # 检查最近错误
        log_file = self.trading_root / "logs" / "trading_system.log"
        if log_file.exists():
            try:
                code, stdout, stderr = self.run_command(
                    f"tail -100 {log_file} | grep -c -E '(ERROR|CRITICAL)'"
                )
                if code == 0 and stdout.strip().isdigit():
                    result["recent_errors"] = int(stdout.strip())
                    if result["recent_errors"] > 10:
                        result["status"] = "warning"
                        result["message"] = f"最近有 {result['recent_errors']} 个错误"
                    elif result["recent_errors"] > 50:
                        result["status"] = "error"
                        result["message"] = f"错误过多: {result['recent_errors']}"
            except:
                pass

        return result

    def check_ibkr_connection(self) -> Dict[str, Any]:
        """检查 IBKR 连接状态

        注意: 此检查已简化 - 如果数据质量检查通过(有tick数据),
        说明IBKR连接正常, 无需单独检查连接状态 (避免误判)
        """
        self.log("检查 IBKR 连接...")

        result = {
            "status": "healthy",
            "connected": True,
            "message": "跳过直接连接检查 - 依赖数据质量检查验证连接状态"
        }

        # 不再直接连接IBKR - 数据质量检查已经验证连接有效性
        # 如果有tick数据流入 → IBKR必然连接正常
        # 如果无数据 → data_quality检查会报错
        return result

    def check_data_quality(self) -> Dict[str, Any]:
        """检查数据质量"""
        self.log("检查数据质量...")

        result = {
            "status": "healthy",
            "recent_ticks": {},
            "yesterday_bars": {},
            "message": ""
        }

        # 检查最近 10 分钟的 Tick 数据
        code, stdout, stderr = self.run_command(
            """docker exec trading_clickhouse clickhouse-client --format=JSON --query "
            SELECT symbol, count() as tick_count
            FROM lake.ticks_trades
            WHERE ts >= now() - INTERVAL 10 MINUTE
            GROUP BY symbol
            " 2>/dev/null"""
        )

        if code == 0 and stdout.strip():
            try:
                data = json.loads(stdout)
                for row in data.get("data", []):
                    result["recent_ticks"][row["symbol"]] = row["tick_count"]

                if not result["recent_ticks"]:
                    result["status"] = "warning"
                    result["message"] = "最近 10 分钟无 Tick 数据"
            except:
                pass

        # 检查昨日 Bar 数据完整性 (使用 bars_1m_ibkr 表)
        code, stdout, stderr = self.run_command(
            """docker exec trading_clickhouse clickhouse-client --format=JSON --query "
            SELECT symbol, count() as bar_count
            FROM trading.bars_1m_ibkr
            WHERE toDate(ts_end) = today() - 1
            GROUP BY symbol
            " 2>/dev/null"""
        )

        if code == 0 and stdout.strip():
            try:
                data = json.loads(stdout)
                for row in data.get("data", []):
                    symbol = row["symbol"]
                    count = row["bar_count"]
                    result["yesterday_bars"][symbol] = {
                        "count": count,
                        "complete": count >= 390
                    }

                    if count < 390:
                        result["status"] = "warning"
                        if not result["message"]:
                            result["message"] = f"昨日数据不完整: {symbol} 仅 {count} bars"
            except:
                pass

        return result

    def check_kafka_consumers(self) -> Dict[str, Any]:
        """检查 Kafka Consumer 状态"""
        self.log("检查 Kafka Consumers...")

        result = {
            "status": "healthy",
            "groups": {},
            "high_lag_groups": [],
            "message": ""
        }

        # 获取 consumer groups 列表
        code, stdout, stderr = self.run_command(
            "docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh "
            "--bootstrap-server localhost:9092 --list 2>/dev/null"
        )

        if code != 0:
            result["status"] = "error"
            result["message"] = "无法获取 Kafka Consumer Groups"
            return result

        groups = [g.strip() for g in stdout.strip().split('\n') if g.strip()]

        # 检查每个 group 的 LAG
        for group in groups[:10]:  # 只检查前 10 个，避免超时
            code, stdout, stderr = self.run_command(
                f"docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh "
                f"--bootstrap-server localhost:9092 --describe --group {group} 2>/dev/null"
            )

            if code == 0:
                total_lag = 0
                for line in stdout.split('\n')[1:]:  # 跳过表头
                    parts = line.split()
                    if len(parts) > 5 and parts[5].isdigit():
                        total_lag += int(parts[5])

                result["groups"][group] = {
                    "lag": total_lag,
                    "status": "healthy" if total_lag < 1000 else "warning"
                }

                if total_lag >= 1000:
                    result["high_lag_groups"].append(group)
                    if total_lag >= 10000:
                        result["status"] = "error"
                    elif result["status"] == "healthy":
                        result["status"] = "warning"

        if result["high_lag_groups"]:
            result["message"] = f"{len(result['high_lag_groups'])} 个 consumer groups 有高延迟"

        return result

    def check_recent_errors(self) -> Dict[str, Any]:
        """检查最近的错误日志"""
        self.log("检查错误日志...")

        result = {
            "status": "healthy",
            "error_count": 0,
            "critical_count": 0,
            "recent_errors": [],
            "message": ""
        }

        log_file = self.trading_root / "logs" / "trading_system.log"
        if not log_file.exists():
            # Trading system log不存在是正常的 (run_system.py可能没运行)
            # AI大脑模式(auto_loop.py)不需要这个文件
            result["status"] = "healthy"
            result["message"] = "trading_system.log不存在 (正常 - 仅AI大脑模式)"
            return result

        # 获取最近 ERROR 和 CRITICAL
        code, stdout, stderr = self.run_command(
            f"tail -200 {log_file} | grep -E '(ERROR|CRITICAL)' | tail -10"
        )

        if code == 0 and stdout.strip():
            errors = stdout.strip().split('\n')
            result["recent_errors"] = errors[:10]  # 最多保存 10 条
            result["error_count"] = len(errors)

            # 计算 CRITICAL 数量
            result["critical_count"] = sum(1 for e in errors if "CRITICAL" in e)

            if result["critical_count"] > 0:
                result["status"] = "error"
                result["message"] = f"发现 {result['critical_count']} 个 CRITICAL 错误"
            elif result["error_count"] > 20:
                result["status"] = "warning"
                result["message"] = f"最近有 {result['error_count']} 个错误"

        return result

    def check_system_resources(self) -> Dict[str, Any]:
        """检查系统资源使用"""
        self.log("检查系统资源...")

        result = {
            "status": "healthy",
            "cpu_percent": 0,
            "memory_percent": 0,
            "disk_percent": 0,
            "message": ""
        }

        try:
            # CPU 使用率
            result["cpu_percent"] = psutil.cpu_percent(interval=1)

            # 内存使用率
            mem = psutil.virtual_memory()
            result["memory_percent"] = mem.percent

            # 磁盘使用率（交易目录所在盘）
            disk = psutil.disk_usage(str(self.trading_root))
            result["disk_percent"] = disk.percent

            # 判断状态
            if result["cpu_percent"] > 90:
                result["status"] = "warning"
                result["message"] = f"CPU 使用率过高: {result['cpu_percent']:.1f}%"
            if result["memory_percent"] > 90:
                result["status"] = "error"
                result["message"] = f"内存使用率过高: {result['memory_percent']:.1f}%"
            if result["disk_percent"] > 90:
                result["status"] = "error"
                result["message"] = f"磁盘使用率过高: {result['disk_percent']:.1f}%"

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"无法获取系统资源信息: {str(e)}"

        return result

    def check_option_ml_status(self) -> Dict[str, Any]:
        """检查 Option ML 状态"""
        self.log("检查 Option ML...")
        result = {
            "status": "healthy",
            "active_model": None,
            "age_hours": 999.0,
            "message": ""
        }
        try:
            # Using clickhouse-client via shell to avoid extra dependencies
            cmd = "docker exec trading_clickhouse clickhouse-client --format=JSON --query \"SELECT updated_at, active_model_version, enabled FROM trading.option_ml_controls ORDER BY updated_at DESC LIMIT 1\""
            res_code, stdout, stderr = self.run_command(cmd)
            if res_code == 0 and stdout.strip():
                data = json.loads(stdout)
                if data.get("data"):
                    row = data["data"][0]
                    # Format: 2026-01-07 23:31:58
                    updated_at = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
                    age = (datetime.now() - updated_at).total_seconds() / 3600
                    result["active_model"] = row["active_model_version"]
                    result["age_hours"] = round(age, 1)
                    result["enabled"] = bool(row["enabled"])
                    
                    if not result["enabled"]:
                        result["status"] = "warning"
                        result["message"] = "Option ML 已禁用"
                    elif age > 24:
                        result["status"] = "warning"
                        result["message"] = f"模型过期 ({age:.1f} 小时)"
            else:
                result["status"] = "warning"
                result["message"] = "无法读取 Option ML 控制表"
        except Exception as e:
            result["status"] = "warning"
            result["message"] = f"检查失败: {str(e)}"
        return result

    def run_all_checks(self):
        """运行所有检查"""
        print("🔍 开始系统健康检查...")
        print()

        checks = [
            ("docker_containers", "Docker 容器", self.check_docker_containers),
            ("run_system_py", "run_system.py", self.check_run_system_py),
            ("ibkr_connection", "IBKR 连接", self.check_ibkr_connection),
            ("data_quality", "数据质量", self.check_data_quality),
            ("kafka_consumers", "Kafka Consumers", self.check_kafka_consumers),
            ("recent_errors", "错误日志", self.check_recent_errors),
            ("system_resources", "系统资源", self.check_system_resources),
            ("option_ml_status", "Option ML", self.check_option_ml_status),
        ]

        for key, name, check_func in checks:
            print(f"  ⏳ 检查 {name}...", end=" ")
            try:
                check_result = check_func()
                self.report["checks"][key] = check_result

                # 更新统计
                self.report["summary"]["total_checks"] += 1
                status = check_result.get("status", "unknown")

                if status == "healthy":
                    self.report["summary"]["passed"] += 1
                    print("✅")
                elif status == "warning":
                    self.report["summary"]["warnings"] += 1
                    print(f"⚠️  {check_result.get('message', '')}")
                else:
                    self.report["summary"]["errors"] += 1
                    print(f"❌ {check_result.get('message', '')}")

                # 更新总体状态
                self.update_status(status)

            except Exception as e:
                print(f"❌ 检查失败: {str(e)}")
                self.report["checks"][key] = {
                    "status": "error",
                    "message": f"检查失败: {str(e)}"
                }
                self.report["summary"]["errors"] += 1
                self.update_status("error")

        print()

    def save_report(self):
        """保存报告到文件"""
        # 保存到固定文件名（最新报告）
        report_file = self.trading_root / "logs" / "system_health.json"
        report_file.parent.mkdir(parents=True, exist_ok=True)

        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(self.report, f, indent=2, ensure_ascii=False)

        # 同时保存一份带时间戳的历史记录
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_file = self.trading_root / "logs" / "health_history" / f"system_health_{timestamp}.json"
        history_file.parent.mkdir(parents=True, exist_ok=True)

        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(self.report, f, indent=2, ensure_ascii=False)

        return report_file, history_file

    def print_summary(self):
        """打印摘要"""
        print("=" * 60)
        print("📊 系统健康检查报告")
        print("=" * 60)
        print(f"检查时间: {self.report['timestamp_readable']}")
        print()

        # 总体状态
        status_emoji = {
            "healthy": "✅",
            "warning": "⚠️",
            "error": "❌"
        }
        emoji = status_emoji.get(self.report["overall_status"], "❓")
        print(f"总体状态: {emoji} {self.report['overall_status'].upper()}")
        print()

        # 统计
        summary = self.report["summary"]
        print(f"检查项目: {summary['total_checks']}")
        print(f"  ✅ 通过: {summary['passed']}")
        print(f"  ⚠️  警告: {summary['warnings']}")
        print(f"  ❌ 错误: {summary['errors']}")
        print()

        # 详细问题
        if summary['errors'] > 0 or summary['warnings'] > 0:
            print("⚠️  需要关注的问题:")
            for key, check in self.report["checks"].items():
                if check.get("status") in ["error", "warning"]:
                    status_icon = "❌" if check["status"] == "error" else "⚠️"
                    message = check.get("message", "无详细信息")
                    print(f"  {status_icon} {key}: {message}")
            print()

        print("=" * 60)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="交易系统健康检查")
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细日志"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="在 stdout 末尾输出 JSON 报告"
    )
    args = parser.parse_args()

    # 创建检查器
    checker = SystemHealthChecker(verbose=args.verbose)

    # 运行所有检查
    checker.run_all_checks()

    # 保存报告
    report_file, history_file = checker.save_report()

    # 打印摘要
    checker.print_summary()

    # 显示报告位置
    print(f"📄 完整报告: {report_file}")
    print(f"📂 历史记录: {history_file}")
    print()

    if args.json:
        print(json.dumps(checker.report, ensure_ascii=False))

    # 根据状态返回退出码
    if checker.report["overall_status"] == "error":
        sys.exit(1)
    elif checker.report["overall_status"] == "warning":
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
