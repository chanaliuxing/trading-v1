#!/usr/bin/env python3
"""
AI Trader Tools Health Check Script

快速检查所有关键工具是否正常工作，确保 AI Brain 不会因工具问题停止运行。

Usage:
    python scripts/ai_trader/check_tools_health.py
    python scripts/ai_trader/check_tools_health.py --verbose
    python scripts/ai_trader/check_tools_health.py --critical-only
"""

import sys
import importlib
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple


class ToolHealthChecker:
    """工具健康检查器"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.results: Dict[str, List[Tuple[str, bool, str]]] = {}

    def _check_import(self, module_path: str, description: str, critical: bool = True) -> Tuple[str, bool, str]:
        """检查模块能否导入"""
        try:
            importlib.import_module(module_path)
            status = "✅ OK"
            if self.verbose:
                print(f"  {status} {description}")
            return (description, True, "")
        except ImportError as e:
            status = "❌ FAIL" if critical else "⚠️  SKIP"
            error = str(e)
            if self.verbose or critical:
                print(f"  {status} {description}: {error}")
            return (description, False, error)

    def _check_function(self, module_path: str, func_name: str, description: str) -> Tuple[str, bool, str]:
        """检查模块中的函数是否存在"""
        try:
            module = importlib.import_module(module_path)
            if hasattr(module, func_name):
                if self.verbose:
                    print(f"  ✅ OK {description}")
                return (description, True, "")
            else:
                error = f"Missing function: {func_name}"
                print(f"  ❌ FAIL {description}: {error}")
                return (description, False, error)
        except ImportError as e:
            error = f"Import failed: {e}"
            print(f"  ❌ FAIL {description}: {error}")
            return (description, False, error)

    def check_reflection_tools(self):
        """检查反思工具链"""
        print("\n🔍 Checking Reflection Toolchain...")
        results = []

        results.append(self._check_import(
            "scripts.ai_trader.reflection.consistency_job",
            "Consistency Job (决策一致性分析)"
        ))

        results.append(self._check_import(
            "scripts.ai_trader.reflection.analyzer",
            "Analyzer (交易分析)"
        ))

        results.append(self._check_import(
            "scripts.ai_trader.reflection.report_builder",
            "Report Builder (报告生成)"
        ))

        results.append(self._check_function(
            "scripts.ai_trader.evaluators.reflection",
            "extract_claims",
            "Extract Claims (声明提取)"
        ))

        results.append(self._check_function(
            "scripts.ai_trader.evaluators.reflection",
            "check_claims",
            "Check Claims (声明验证)"
        ))

        self.results["Reflection"] = results
        return all(r[1] for r in results)

    def check_portfolio_tools(self):
        """检查投资组合工具"""
        print("\n💼 Checking Portfolio Tools...")
        results = []

        results.append(self._check_import(
            "scripts.ai_trader.portfolio.overlay",
            "Overlay (风险叠加)",
            critical=False
        ))

        results.append(self._check_import(
            "scripts.ai_trader.portfolio.optimizer",
            "Optimizer (组合优化)",
            critical=False
        ))

        self.results["Portfolio"] = results
        return True  # Non-critical

    def check_evaluator_tools(self):
        """检查评估工具"""
        print("\n📊 Checking Evaluator Tools...")
        results = []

        results.append(self._check_import(
            "scripts.ai_trader.evaluators.scorecard",
            "Scorecard (性能评分)"
        ))

        self.results["Evaluators"] = results
        return all(r[1] for r in results)

    def check_context_analyzer(self):
        """检查上下文分析器"""
        print("\n🧠 Checking Context Analyzer...")
        results = []

        results.append(self._check_import(
            "scripts.ai_trader.context.context_analyzer",
            "Context Analyzer (AI 上下文)"
        ))

        self.results["Context"] = results
        return all(r[1] for r in results)

    def check_evolvers(self):
        """检查进化工具"""
        print("\n🧬 Checking Evolver Tools...")
        results = []

        results.append(self._check_import(
            "scripts.ai_trader.evolvers.prompt_evolver",
            "Prompt Evolver (Prompt 优化)"
        ))

        results.append(self._check_import(
            "scripts.ai_trader.evolvers.decision_engine",
            "Decision Engine (决策引擎)"
        ))

        self.results["Evolvers"] = results
        return all(r[1] for r in results)

    def check_rl_tools(self):
        """检查强化学习工具"""
        print("\n🤖 Checking RL Tools...")
        results = []

        results.append(self._check_import(
            "scripts.ai_trader.rl.eval_rl",
            "RL Eval (强化学习评估)"
        ))

        self.results["RL"] = results
        return all(r[1] for r in results)

    def check_brain_dependencies(self):
        """检查 Brain 核心依赖"""
        print("\n🧠 Checking Brain Core Dependencies...")
        results = []

        results.append(self._check_import(
            "scripts.ai_trader.brain.brain",
            "Control Brain (LLM 决策大脑)"
        ))

        results.append(self._check_import(
            "scripts.ai_trader.orchestrator",
            "Orchestrator (工作流编排)"
        ))

        results.append(self._check_import(
            "scripts.ai_trader.memory.episode_store",
            "Episode Store (记忆持久化)"
        ))

        results.append(self._check_import(
            "scripts.ai_trader.memory.aggregator",
            "Memory Aggregator (记忆检索)"
        ))

        self.results["Brain Core"] = results
        return all(r[1] for r in results)

    def check_external_dependencies(self):
        """检查外部依赖"""
        print("\n📦 Checking External Dependencies...")
        results = []

        results.append(self._check_import(
            "llm_trader",
            "LLM Trader (LLM 提供商)"
        ))

        results.append(self._check_import(
            "libs.db.clickhouse",
            "ClickHouse Client",
            critical=False
        ))

        results.append(self._check_import(
            "libs.db.postgres",
            "PostgreSQL Client",
            critical=False
        ))

        self.results["External"] = results
        # ClickHouse/PostgreSQL 是可选的
        return results[0][1]  # Only llm_trader is critical

    def print_summary(self):
        """打印汇总报告"""
        print("\n" + "="*60)
        print("🏥 Health Check Summary")
        print("="*60)

        total_checks = sum(len(results) for results in self.results.values())
        passed_checks = sum(sum(1 for r in results if r[1]) for results in self.results.values())

        for category, results in self.results.items():
            passed = sum(1 for r in results if r[1])
            total = len(results)
            status = "✅" if passed == total else ("⚠️" if passed > 0 else "❌")
            print(f"{status} {category}: {passed}/{total} passed")

        print("-"*60)
        print(f"Total: {passed_checks}/{total_checks} checks passed")

        if passed_checks == total_checks:
            print("\n✅ All tools healthy! AI Brain ready to run.")
            return 0
        elif passed_checks >= total_checks * 0.8:
            print("\n⚠️  Most tools healthy, but some issues detected.")
            return 1
        else:
            print("\n❌ Critical tools failing! AI Brain may not work properly.")
            return 2

    def run_all_checks(self, critical_only: bool = False) -> int:
        """运行所有检查"""
        print("="*60)
        print("🏥 AI Trader Tools Health Check")
        from datetime import timezone
        import pytz
        ny_tz = pytz.timezone('America/New_York')
        print(f"📅 {datetime.now(ny_tz).strftime('%Y-%m-%d %H:%M:%S ET')}")
        print("="*60)

        checks = [
            ("Brain Core", self.check_brain_dependencies),
            ("External", self.check_external_dependencies),
            ("Reflection", self.check_reflection_tools),
            ("Evaluators", self.check_evaluator_tools),
            ("Context", self.check_context_analyzer),
            ("Evolvers", self.check_evolvers),
            ("RL", self.check_rl_tools),
        ]

        if not critical_only:
            checks.append(("Portfolio", self.check_portfolio_tools))

        all_passed = True
        for name, check_func in checks:
            try:
                passed = check_func()
                if not passed:
                    all_passed = False
            except Exception as e:
                print(f"  ❌ FAIL {name}: Unexpected error: {e}")
                all_passed = False

        return self.print_summary()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Check AI Trader tools health")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--critical-only", "-c", action="store_true", help="Only check critical tools")
    args = parser.parse_args()

    checker = ToolHealthChecker(verbose=args.verbose)
    exit_code = checker.run_all_checks(critical_only=args.critical_only)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
