#!/usr/bin/env python3
"""
Patch Evaluator: 自动评估代码补丁的收益和风险

实现蓝皮书 v3 的可验证自编程要求:
1. 自动运行测试 (单元/集成/回归)
2. 静态分析 (ruff, mypy, bandit)
3. 性能对比 (before/after)
4. 计算收益/风险评分
5. 决定是否加入审批队列

Usage:
  PYTHONPATH=. venv/bin/python tools/patch_evaluator.py \
    --patch-id exp_12345 \
    --patch-file runs/ai_trader/patches/exp_12345.diff \
    --target-module tool:backtest_ml_engine
"""
import argparse
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Tuple
import structlog

logger = structlog.get_logger()


class PatchEvaluator:
    """补丁评估器：评估代码补丁的质量和风险"""

    def __init__(self, patch_id: str, patch_file: Path, target_module: str):
        self.patch_id = patch_id
        self.patch_file = patch_file
        self.target_module = target_module
        self.logger = logger.bind(patch_id=patch_id, target=target_module)

        self.evaluation_results = {
            "patch_id": patch_id,
            "target_module": target_module,
            "evaluated_at": datetime.now().isoformat(),
            "stages": {}
        }

    def evaluate_all(self) -> Dict[str, Any]:
        """执行完整评估流程"""
        self.logger.info("patch_evaluation_start")

        # 1. 静态分析
        static_result = self.run_static_analysis()
        self.evaluation_results["stages"]["static_analysis"] = static_result

        # 2. 单元测试
        unit_test_result = self.run_unit_tests()
        self.evaluation_results["stages"]["unit_tests"] = unit_test_result

        # 3. 集成测试
        integration_result = self.run_integration_tests()
        self.evaluation_results["stages"]["integration_tests"] = integration_result

        # 4. 回归测试
        regression_result = self.run_regression_tests()
        self.evaluation_results["stages"]["regression_tests"] = regression_result

        # 5. 性能测试
        performance_result = self.run_performance_tests()
        self.evaluation_results["stages"]["performance_tests"] = performance_result

        # 6. 安全扫描
        security_result = self.run_security_scan()
        self.evaluation_results["stages"]["security_scan"] = security_result

        # 7. 计算综合评分
        scores = self.calculate_scores()
        self.evaluation_results["scores"] = scores

        # 8. 决策
        decision = self.make_decision(scores)
        self.evaluation_results["decision"] = decision

        self.logger.info("patch_evaluation_complete",
                        benefit_score=scores["benefit_score"],
                        risk_score=scores["risk_score"],
                        decision=decision["recommendation"])

        return self.evaluation_results

    def run_static_analysis(self) -> Dict[str, Any]:
        """运行静态分析 (ruff, mypy, bandit)"""
        self.logger.info("static_analysis_start")
        start_time = time.time()

        results = {
            "passed": True,
            "ruff_issues": 0,
            "mypy_issues": 0,
            "bandit_issues": 0,
            "complexity_score": 0.0,
            "details": []
        }

        # 获取补丁影响的文件列表
        affected_files = self._get_affected_files()
        if not affected_files:
            return {
                "passed": True,
                "ruff_issues": 0,
                "mypy_issues": 0,
                "bandit_issues": 0,
                "complexity_score": 0.0,
                "details": [],
                "duration_ms": int((time.time() - start_time) * 1000),
                "message": "No existing affected files found"
            }

        # 1. Ruff (快速 linter)
        ruff_result = self._run_command(["ruff", "check"] + affected_files)
        results["ruff_issues"] = ruff_result.get("issues", 0)
        results["details"].append({"tool": "ruff", "output": ruff_result.get("output", "")})

        # 2. Mypy (类型检查)
        mypy_result = self._run_command(["mypy"] + affected_files)
        results["mypy_issues"] = mypy_result.get("issues", 0)
        results["details"].append({"tool": "mypy", "output": mypy_result.get("output", "")})

        # 3. Bandit (安全扫描)
        bandit_result = self._run_command(["bandit", "-r"] + affected_files)
        results["bandit_issues"] = bandit_result.get("issues", 0)
        results["details"].append({"tool": "bandit", "output": bandit_result.get("output", "")})

        # 4. 复杂度评估 (radon)
        complexity_result = self._run_command(["radon", "cc"] + affected_files + ["-a"])
        results["complexity_score"] = complexity_result.get("complexity", 0.0)

        # 判断是否通过
        results["passed"] = (
            results["ruff_issues"] == 0 and
            results["mypy_issues"] == 0 and
            results["bandit_issues"] <= 1 and  # 允许1个低危安全问题
            results["complexity_score"] < 15.0  # 复杂度阈值
        )

        results["duration_ms"] = int((time.time() - start_time) * 1000)
        self.logger.info("static_analysis_complete", **results)

        return results

    def run_unit_tests(self) -> Dict[str, Any]:
        """运行单元测试"""
        self.logger.info("unit_tests_start")
        start_time = time.time()

        # 确定测试文件
        test_files = self._find_test_files(self._get_affected_files())

        if not test_files:
            return {
                "passed": True,
                "total": 0,
                "failed": 0,
                "coverage": 0.0,
                "duration_ms": int((time.time() - start_time) * 1000),
                "message": "No tests found"
            }

        # 运行 pytest
        result = self._run_command([
            "pytest",
            "-v",
            "--cov=" + self.target_module.split(":")[1] if ":" in self.target_module else "",
            "--cov-report=json"
        ] + test_files)

        return {
            "passed": result.get("returncode", 1) == 0,
            "total": result.get("total_tests", 0),
            "failed": result.get("failed_tests", 0),
            "coverage": result.get("coverage", 0.0),
            "duration_ms": int((time.time() - start_time) * 1000)
        }

    def run_integration_tests(self) -> Dict[str, Any]:
        """运行集成测试"""
        self.logger.info("integration_tests_start")
        start_time = time.time()

        # Run only related integration tests to avoid evaluating the whole suite.
        affected_files = self._get_affected_files()
        integration_tests = self._find_related_integration_tests(affected_files)
        if not integration_tests:
            return {
                "passed": True,
                "total": 0,
                "duration_ms": int((time.time() - start_time) * 1000),
                "message": "No related integration tests found"
            }

        result = self._run_command(["pytest", "-v"] + integration_tests)

        return {
            "passed": result.get("returncode", 1) == 0,
            "total": result.get("total_tests", 0),
            "failed": result.get("failed_tests", 0),
            "duration_ms": int((time.time() - start_time) * 1000)
        }

    def run_regression_tests(self) -> Dict[str, Any]:
        """运行回归测试 (对比 before/after 结果)"""
        self.logger.info("regression_tests_start")
        start_time = time.time()

        # 对于 trading domain，可以运行 backtest 对比
        if self.target_module.startswith("tool:backtest"):
            # 运行 before/after backtest
            before_metrics = self._run_backtest(use_patch=False)
            after_metrics = self._run_backtest(use_patch=True)

            # 对比关键指标
            regressions = self._detect_regressions(before_metrics, after_metrics)

            return {
                "passed": len(regressions) == 0,
                "regressions": regressions,
                "before_metrics": before_metrics,
                "after_metrics": after_metrics,
                "duration_ms": int((time.time() - start_time) * 1000)
            }

        return {
            "passed": True,
            "message": "No regression tests applicable",
            "duration_ms": int((time.time() - start_time) * 1000)
        }

    def run_performance_tests(self) -> Dict[str, Any]:
        """运行性能测试 (测量延迟、吞吐量)"""
        self.logger.info("performance_tests_start")
        start_time = time.time()

        # 简化实现：对比 before/after 执行时间
        before_time = self._measure_execution_time(use_patch=False)
        after_time = self._measure_execution_time(use_patch=True)

        improvement_pct = ((before_time - after_time) / before_time * 100) if before_time > 0 else 0

        return {
            "passed": improvement_pct >= -5.0,  # 允许性能退化 ≤5%
            "before_ms": before_time,
            "after_ms": after_time,
            "improvement_pct": improvement_pct,
            "duration_ms": int((time.time() - start_time) * 1000)
        }

    def run_security_scan(self) -> Dict[str, Any]:
        """运行安全扫描 (已在静态分析中包含 bandit)"""
        # 这里可以添加更多安全工具，如 safety, semgrep 等
        return {
            "passed": True,
            "message": "Security scan completed in static_analysis stage"
        }

    def calculate_scores(self) -> Dict[str, float]:
        """计算收益和风险评分"""
        # 收益评分 (0-1)
        benefit_factors = []

        # 1. 性能改善
        perf = self.evaluation_results["stages"].get("performance_tests", {})
        if perf.get("improvement_pct", 0) > 0:
            benefit_factors.append(min(1.0, perf["improvement_pct"] / 20.0))  # 20% improvement = 1.0

        # 2. 测试覆盖率
        unit = self.evaluation_results["stages"].get("unit_tests", {})
        if unit.get("coverage", 0) > 0:
            benefit_factors.append(unit["coverage"])

        # 3. 回归测试通过
        regression = self.evaluation_results["stages"].get("regression_tests", {})
        if regression.get("passed", False):
            benefit_factors.append(0.5)

        benefit_score = sum(benefit_factors) / max(len(benefit_factors), 1) if benefit_factors else 0.0

        # 风险评分 (0-1, 越高越危险)
        risk_factors = []

        # 1. 静态分析问题
        static = self.evaluation_results["stages"].get("static_analysis", {})
        issue_count = static.get("ruff_issues", 0) + static.get("mypy_issues", 0) + static.get("bandit_issues", 0)
        risk_factors.append(min(1.0, issue_count / 10.0))

        # 2. 测试失败
        if not unit.get("passed", True):
            risk_factors.append(0.8)

        # 3. 复杂度高
        if static.get("complexity_score", 0) > 10:
            risk_factors.append(min(1.0, static["complexity_score"] / 20.0))

        # 4. 性能退化
        if perf.get("improvement_pct", 0) < -5:
            risk_factors.append(min(1.0, abs(perf["improvement_pct"]) / 20.0))

        risk_score = sum(risk_factors) / max(len(risk_factors), 1) if risk_factors else 0.0

        # 置信度 (基于测试覆盖率和样本大小)
        confidence = unit.get("coverage", 0) * 0.7 + (0.3 if unit.get("total", 0) >= 5 else 0.0)

        return {
            "benefit_score": round(benefit_score, 3),
            "risk_score": round(risk_score, 3),
            "confidence": round(confidence, 3),
            "net_score": round(benefit_score - risk_score, 3)
        }

    def make_decision(self, scores: Dict[str, float]) -> Dict[str, Any]:
        """基于评分做出决策"""
        benefit = scores["benefit_score"]
        risk = scores["risk_score"]
        confidence = scores["confidence"]
        net_score = scores["net_score"]

        # 决策规则
        if net_score >= 0.3 and risk <= 0.3 and confidence >= 0.6:
            recommendation = "auto_approve"
            reason = "High benefit, low risk, high confidence"
        elif net_score >= 0.2 and risk <= 0.5:
            recommendation = "manual_review"
            reason = "Moderate benefit/risk, needs human review"
        elif risk >= 0.7:
            recommendation = "reject"
            reason = "High risk"
        else:
            recommendation = "manual_review"
            reason = "Insufficient confidence"

        return {
            "recommendation": recommendation,
            "reason": reason,
            "can_auto_apply": recommendation == "auto_approve"
        }

    # ===== Helper Methods =====

    def _get_affected_files(self) -> List[str]:
        """从 patch 文件中提取影响的文件列表"""
        if not self.patch_file.exists():
            return []

        affected = []
        with open(self.patch_file) as f:
            for line in f:
                if line.startswith("+++") or line.startswith("---"):
                    raw_path = line.split()[1].strip()
                    if raw_path == "/dev/null":
                        continue
                    # Normalize git diff prefixes (a/ and b/).
                    if raw_path.startswith("a/") or raw_path.startswith("b/"):
                        raw_path = raw_path[2:]
                    p = Path(raw_path)
                    if p.exists():
                        affected.append(str(p))
        return list(set(affected))

    def _find_related_integration_tests(self, source_files: List[str]) -> List[str]:
        """Find integration tests related to changed source files by stem/path hints."""
        candidates = list(Path("tests/integration").glob("test_*.py"))
        if not candidates:
            return []
        if not source_files:
            return []
        name_hints = {Path(src).stem.lower() for src in source_files if src}
        path_hints = {p.lower() for src in source_files for p in Path(src).parts}
        selected: List[str] = []
        for test_file in candidates:
            tname = test_file.stem.lower()
            tpath = str(test_file).lower()
            if any(h and h in tname for h in name_hints):
                selected.append(str(test_file))
                continue
            if any(h and h in tpath for h in path_hints):
                selected.append(str(test_file))
        return sorted(set(selected))

    def _find_test_files(self, source_files: List[str]) -> List[str]:
        """查找对应的测试文件"""
        test_files = []
        for src_file in source_files:
            # 简单规则: src/foo.py -> tests/test_foo.py
            test_path = Path("tests") / f"test_{Path(src_file).name}"
            if test_path.exists():
                test_files.append(str(test_path))
        return test_files

    def _run_command(self, cmd: List[str]) -> Dict[str, Any]:
        """运行命令并解析结果"""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            return {
                "returncode": result.returncode,
                "output": result.stdout,
                "issues": result.stdout.count("error:") + result.stdout.count("warning:"),
            }
        except Exception as e:
            return {"returncode": 1, "output": str(e), "issues": 999}

    def _run_backtest(self, use_patch: bool) -> Dict[str, float]:
        """运行 backtest (简化实现)"""
        # Placeholder: 实际应该调用 backtest 引擎
        return {"sharpe": 1.5, "max_drawdown": 0.15, "trades": 100}

    def _detect_regressions(self, before: Dict, after: Dict) -> List[str]:
        """检测回归 (性能退化)"""
        regressions = []
        if after.get("sharpe", 0) < before.get("sharpe", 0) * 0.9:
            regressions.append("sharpe degraded")
        if after.get("max_drawdown", 0) > before.get("max_drawdown", 1.0) * 1.1:
            regressions.append("max_drawdown increased")
        return regressions

    def _measure_execution_time(self, use_patch: bool) -> float:
        """测量执行时间 (毫秒)"""
        # Placeholder
        return 100.0


def main():
    ap = argparse.ArgumentParser(description="Evaluate code patch")
    ap.add_argument("--patch-id", required=True, help="Patch ID")
    ap.add_argument("--patch-file", required=True, help="Path to patch file")
    ap.add_argument("--target-module", required=True, help="Target module")
    ap.add_argument("--output", default=None, help="Output JSON file")
    args = ap.parse_args()

    evaluator = PatchEvaluator(
        patch_id=args.patch_id,
        patch_file=Path(args.patch_file),
        target_module=args.target_module
    )

    results = evaluator.evaluate_all()

    # 输出结果
    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2))
        print(f"✅ Evaluation results written to {args.output}")
    else:
        print(json.dumps(results, indent=2))

    # 返回退出码
    if results["decision"]["recommendation"] == "reject":
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
