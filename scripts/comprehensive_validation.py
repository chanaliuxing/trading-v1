#!/usr/bin/env python3
"""
综合验证脚本 - 验证所有 P0/P2 改进

这个脚本运行所有验证检查，确保系统改进正确实施：
1. P0: Brain 接入 ClickHouse 模型战绩
2. P0: ML 服务生产环境禁用
3. P0: Model gating 单元测试
4. P2: 配置完整性检查
5. P2: Fail-Fast 代码质量检查

Usage:
    python scripts/comprehensive_validation.py [--full]

Options:
    --full: 运行完整验证（包括耗时的测试）
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple, Dict
import time


class ComprehensiveValidator:
    """综合验证器"""

    def __init__(self, full_mode: bool = False):
        self.full_mode = full_mode
        self.results: Dict[str, Tuple[bool, str]] = {}
        self.start_time = time.time()

    def run_all(self) -> bool:
        """运行所有验证"""
        print("\n" + "="*70)
        print("🔍 Trading System - 综合验证")
        print("="*70 + "\n")

        # 1. P0 验证
        self._print_section("P0 修复验证")
        self._check_brain_ch_integration()
        self._check_production_blocks()
        self._check_model_gating_tests()

        # 2. P2 验证
        self._print_section("P2 改进验证")
        self._check_config_sanity()
        self._check_fail_fast_quality()

        # 3. 代码质量
        if self.full_mode:
            self._print_section("代码质量检查")
            self._check_syntax_all()
            self._run_test_suite()

        # 4. 文档完整性
        self._print_section("文档完整性")
        self._check_documentation()

        # 打印汇总
        self._print_summary()

        # 返回结果
        all_passed = all(result[0] for result in self.results.values())
        return all_passed

    def _print_section(self, title: str):
        """打印节标题"""
        print(f"\n{'─'*70}")
        print(f"  {title}")
        print(f"{'─'*70}\n")

    def _run_command(self, cmd: List[str], check_name: str, timeout: int = 60) -> Tuple[bool, str]:
        """运行命令并记录结果"""
        print(f"运行: {check_name}...", end=" ", flush=True)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=Path(__file__).parent.parent  # 项目根目录
            )
            success = result.returncode == 0
            output = result.stdout + result.stderr

            if success:
                print("✅")
            else:
                print("❌")
                print(f"  错误: {output[:200]}")

            self.results[check_name] = (success, output)
            return success, output

        except subprocess.TimeoutExpired:
            print("⏱️ 超时")
            self.results[check_name] = (False, "Command timeout")
            return False, "Command timeout"
        except Exception as e:
            print(f"❌ 异常: {e}")
            self.results[check_name] = (False, str(e))
            return False, str(e)

    def _check_brain_ch_integration(self):
        """验证 Brain 接入 ClickHouse"""
        # 检查函数是否存在
        check_file = Path("scripts/ai_trader/control_event_consumer.py")
        if not check_file.exists():
            self.results["Brain CH 集成 - 文件存在"] = (False, "File not found")
            print("❌ control_event_consumer.py 不存在")
            return

        content = check_file.read_text()

        # 检查关键函数
        checks = [
            ("_get_model_confidence_multiplier 函数", "def _get_model_confidence_multiplier("),
            ("导入 get_model_stats_summary_from_ch", "get_model_stats_summary_from_ch"),
            ("Gating 逻辑集成", "model_gating"),
        ]

        for check_name, pattern in checks:
            exists = pattern in content
            status = "✅" if exists else "❌"
            print(f"  {status} {check_name}")
            self.results[f"Brain CH - {check_name}"] = (exists, "Found" if exists else "Not found")

    def _check_production_blocks(self):
        """验证生产环境服务禁用"""
        # 运行生产禁用测试
        test_file = Path("tests/integration/test_production_blocks.py")
        if test_file.exists():
            self._run_command(
                ["python", str(test_file)],
                "生产禁用测试",
                timeout=30
            )
        else:
            print("  ⚠️  生产禁用测试文件不存在")
            self.results["生产禁用测试"] = (False, "Test file not found")

    def _check_model_gating_tests(self):
        """验证 Model Gating 单元测试"""
        self._run_command(
            ["pytest", "tests/unit/ai_trader/test_model_gating.py", "-v", "--tb=short"],
            "Model Gating 单元测试",
            timeout=60
        )

    def _check_config_sanity(self):
        """验证配置完整性"""
        self._run_command(
            ["python", "scripts/config_sanity_check.py", "--config", "config.yml"],
            "配置完整性检查",
            timeout=30
        )

    def _check_fail_fast_quality(self):
        """验证 Fail-Fast 代码质量"""
        success, output = self._run_command(
            ["python", "scripts/enforce_fail_fast.py", "--path", "."],
            "Fail-Fast 代码质量",
            timeout=60
        )

        # 解析输出
        if success or "总计:" in output:
            lines = output.split('\n')
            for line in lines:
                if "总计:" in line:
                    print(f"  ℹ️  {line.strip()}")

    def _check_syntax_all(self):
        """检查所有 Python 文件语法"""
        print("  检查所有 Python 文件语法...", end=" ", flush=True)

        py_files = [
            "scripts/ai_trader/control_event_consumer.py",
            "services/ml/real_inference_service.py",
            "services/ml/mock_inference_service.py",
            "scripts/config_sanity_check.py",
            "scripts/enforce_fail_fast.py",
        ]

        all_ok = True
        for py_file in py_files:
            path = Path(py_file)
            if not path.exists():
                continue

            try:
                result = subprocess.run(
                    ["python", "-m", "py_compile", str(path)],
                    capture_output=True,
                    timeout=10
                )
                if result.returncode != 0:
                    all_ok = False
                    print(f"\n  ❌ {py_file}: {result.stderr.decode()[:100]}")
            except Exception as e:
                all_ok = False
                print(f"\n  ❌ {py_file}: {e}")

        if all_ok:
            print("✅")
        else:
            print("❌")

        self.results["语法检查"] = (all_ok, "All files OK" if all_ok else "Some files failed")

    def _run_test_suite(self):
        """运行完整测试套件（仅在 --full 模式）"""
        self._run_command(
            ["pytest", "tests/", "-x", "--tb=short", "-q",
             "--ignore=tests/integration/test_rl_evaluation.py",
             "--ignore=tests/unit/strategies/test_enhanced_model_strategy.py"],
            "完整测试套件",
            timeout=300
        )

    def _check_documentation(self):
        """检查文档完整性"""
        required_docs = [
            ("P0 Gap 分析", "doc/analysis/phase1_gap_analysis_20251119.md"),
            ("P0 修复方案", "doc/analysis/p0_fix_plan_20251119.md"),
            ("P0 执行报告", "doc/analysis/p0_execution_report_20251119.md"),
            ("ML Service 审计", "doc/analysis/ml_service_audit_20251119.md"),
            ("数据源审计", "doc/analysis/phase4b_datasource_audit_20251119.md"),
            ("生产禁用报告", "doc/analysis/phase4c_production_blocks_report_20251119.md"),
            ("测试报告", "doc/analysis/phase4d_testing_report_20251119.md"),
            ("完整总结", "doc/analysis/p0_complete_summary_20251119.md"),
        ]

        all_exist = True
        for doc_name, doc_path in required_docs:
            exists = Path(doc_path).exists()
            status = "✅" if exists else "❌"
            print(f"  {status} {doc_name}")
            if not exists:
                all_exist = False
            self.results[f"文档 - {doc_name}"] = (exists, "Exists" if exists else "Missing")

        if all_exist:
            print("\n  ✅ 所有文档齐全")

    def _print_summary(self):
        """打印汇总"""
        elapsed = time.time() - self.start_time

        print("\n" + "="*70)
        print("📊 验证结果汇总")
        print("="*70 + "\n")

        passed = sum(1 for result in self.results.values() if result[0])
        total = len(self.results)

        print(f"通过: {passed}/{total}")
        print(f"耗时: {elapsed:.1f}秒\n")

        # 分类显示
        if passed < total:
            print("❌ 未通过的检查:")
            for check_name, (success, _) in self.results.items():
                if not success:
                    print(f"  - {check_name}")
            print()

        # 总体结论
        if passed == total:
            print("✅ 🎉 所有验证通过！系统改进已成功实施。")
        elif passed >= total * 0.8:
            print("⚠️  大部分验证通过，但仍有少数问题需要修复。")
        else:
            print("❌ 发现多个问题，请修复后重新验证。")

        print("="*70 + "\n")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Trading System - 综合验证脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--full",
        action="store_true",
        help="运行完整验证（包括完整测试套件）"
    )

    args = parser.parse_args()

    # 运行验证
    validator = ComprehensiveValidator(full_mode=args.full)
    success = validator.run_all()

    # 退出码
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
