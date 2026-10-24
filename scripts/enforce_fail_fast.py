#!/usr/bin/env python3
"""
Enforce Fail-Fast - 去掉 Silent Fallback，让配置错误立即暴露

这个脚本扫描代码库中的 silent fallback 模式，帮助识别需要改进的地方。

Silent Fallback 反模式示例:
    ❌ config = data.get("config", {})  # 配置缺失时悄悄用空dict
    ❌ writer = self.writers.get(symbol) or self.default_writer  # 悄悄降级
    ❌ try: ... except: pass  # 吞掉异常

Fail-Fast 最佳实践:
    ✅ config = data["config"]  # 缺失时抛 KeyError
    ✅ if symbol not in self.writers:
           raise ConfigurationError(f"No writer configured for {symbol}")
    ✅ try: ... except SpecificError as e:
           logger.error(..., exc_info=True)
           raise  # 重新抛出

Usage:
    python scripts/enforce_fail_fast.py [--path PATH] [--fix]
"""

import argparse
import re
from pathlib import Path
from typing import List, Dict, Tuple
import sys


class FailFastEnforcer:
    """Fail-Fast 强制器"""

    def __init__(self, root_path: str = "."):
        self.root_path = Path(root_path)
        self.issues: List[Dict] = []

    def scan_all(self) -> List[Dict]:
        """扫描所有 Python 文件"""
        print(f"\n扫描目录: {self.root_path}")
        print(f"{'='*70}\n")

        # 扫描关键目录
        scan_dirs = [
            "scripts/ai_trader",
            "services",
            "strategies",
            "libs",
        ]

        for scan_dir in scan_dirs:
            dir_path = self.root_path / scan_dir
            if not dir_path.exists():
                continue

            print(f"扫描 {scan_dir}/...")
            py_files = list(dir_path.rglob("*.py"))
            for py_file in py_files:
                self._scan_file(py_file)

        return self.issues

    def _scan_file(self, file_path: Path):
        """扫描单个文件"""
        try:
            content = file_path.read_text(encoding='utf-8')
            lines = content.splitlines()

            for line_no, line in enumerate(lines, 1):
                # 检查各种 silent fallback 模式
                self._check_dict_get_with_default(file_path, line_no, line)
                self._check_bare_except(file_path, line_no, line)
                self._check_or_empty_dict(file_path, line_no, line)

        except Exception as e:
            print(f"  警告: 无法读取 {file_path}: {e}")

    def _check_dict_get_with_default(self, file_path: Path, line_no: int, line: str):
        """检查 .get(key, default) 模式"""
        # 匹配 .get("xxx", {}) 或 .get("xxx", []) 或 .get("xxx", None)
        pattern = r'\.get\(["\'][\w_]+["\']\s*,\s*(\{\}|\[\]|None|\d+|"[^"]*"|\'[^\']*\')\)'
        matches = re.findall(pattern, line)

        if matches and "# fallback ok" not in line.lower():
            # 排除一些合理的 fallback
            if ".get(" in line and any(ok in line for ok in ["logger.", "log.", "print(", "#"]):
                return  # 日志相关的 fallback 是可以的

            self.issues.append({
                "file": str(file_path),
                "line": line_no,
                "type": "dict_get_fallback",
                "severity": "warning",
                "message": f"Silent fallback: {line.strip()}",
                "suggestion": "考虑使用 dict[key] 直接访问，或明确检查 if key in dict"
            })

    def _check_bare_except(self, file_path: Path, line_no: int, line: str):
        """检查 bare except"""
        if re.match(r'\s*except\s*:', line):
            self.issues.append({
                "file": str(file_path),
                "line": line_no,
                "type": "bare_except",
                "severity": "error",
                "message": f"Bare except: {line.strip()}",
                "suggestion": "使用 except SpecificException as e: 并重新抛出或记录"
            })

    def _check_or_empty_dict(self, file_path: Path, line_no: int, line: str):
        """检查 or {} / or [] 模式"""
        if (" or {}" in line or " or []" in line) and "# fallback ok" not in line.lower():
            self.issues.append({
                "file": str(file_path),
                "line": line_no,
                "type": "or_empty_container",
                "severity": "warning",
                "message": f"Or empty container: {line.strip()}",
                "suggestion": "考虑显式检查 if value is None"
            })

    def print_report(self):
        """打印报告"""
        print(f"\n{'='*70}")
        print("Fail-Fast 检查报告")
        print(f"{'='*70}\n")

        if not self.issues:
            print("✅ 未发现 silent fallback 模式")
            return

        # 按严重性分组
        errors = [i for i in self.issues if i["severity"] == "error"]
        warnings = [i for i in self.issues if i["severity"] == "warning"]

        if errors:
            print(f"❌ 发现 {len(errors)} 个错误 (必须修复):\n")
            for i, issue in enumerate(errors[:10], 1):  # 只显示前 10 个
                print(f"  {i}. {issue['file']}:{issue['line']}")
                print(f"     {issue['message']}")
                print(f"     → {issue['suggestion']}\n")

            if len(errors) > 10:
                print(f"  ... 还有 {len(errors) - 10} 个错误\n")

        if warnings:
            print(f"⚠️  发现 {len(warnings)} 个警告 (建议修复):\n")
            for i, issue in enumerate(warnings[:10], 1):  # 只显示前 10 个
                print(f"  {i}. {issue['file']}:{issue['line']}")
                print(f"     {issue['message']}")
                print(f"     → {issue['suggestion']}\n")

            if len(warnings) > 10:
                print(f"  ... 还有 {len(warnings) - 10} 个警告\n")

        print(f"{'='*70}")
        print(f"总计: {len(errors)} 个错误, {len(warnings)} 个警告")
        print(f"{'='*70}\n")

        # 统计每种类型
        type_counts = {}
        for issue in self.issues:
            t = issue["type"]
            type_counts[t] = type_counts.get(t, 0) + 1

        print("问题类型分布:")
        for issue_type, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"  - {issue_type}: {count}")

    def generate_fix_recommendations(self) -> str:
        """生成修复建议文档"""
        recommendations = []
        recommendations.append("# Fail-Fast 修复建议\n")
        recommendations.append("## 1. 配置相关 Fallback\n")
        recommendations.append("### 修复前:\n")
        recommendations.append("```python")
        recommendations.append('config = data.get("config", {})')
        recommendations.append('writer = self.writers.get(symbol) or default_writer')
        recommendations.append("```\n")
        recommendations.append("### 修复后:\n")
        recommendations.append("```python")
        recommendations.append('if "config" not in data:')
        recommendations.append('    raise ConfigurationError("Missing required config")')
        recommendations.append('config = data["config"]\n')
        recommendations.append('if symbol not in self.writers:')
        recommendations.append('    raise ConfigurationError(f"No writer configured for {symbol}")')
        recommendations.append('writer = self.writers[symbol]')
        recommendations.append("```\n")

        recommendations.append("## 2. 异常处理\n")
        recommendations.append("### 修复前:\n")
        recommendations.append("```python")
        recommendations.append("try:")
        recommendations.append("    result = risky_operation()")
        recommendations.append("except:")
        recommendations.append("    pass  # Silent failure")
        recommendations.append("```\n")
        recommendations.append("### 修复后:\n")
        recommendations.append("```python")
        recommendations.append("try:")
        recommendations.append("    result = risky_operation()")
        recommendations.append("except ValueError as e:")
        recommendations.append('    logger.error("operation_failed", error=str(e), exc_info=True)')
        recommendations.append("    raise  # Re-raise")
        recommendations.append("```\n")

        recommendations.append("## 3. 可选的 Fallback (OK)\n")
        recommendations.append("有些 fallback 是合理的，可以保留:\n")
        recommendations.append("```python")
        recommendations.append('# 日志相关 - OK')
        recommendations.append('logger.debug("message", extra=data.get("extra", {}))')
        recommendations.append("")
        recommendations.append('# UI 显示 - OK')
        recommendations.append('display_name = user.get("name") or "Anonymous"')
        recommendations.append("")
        recommendations.append('# 性能优化缓存 - OK')
        recommendations.append('cached_value = cache.get(key) or compute_expensive()')
        recommendations.append("```\n")

        return "\n".join(recommendations)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Enforce Fail-Fast: 扫描并报告 silent fallback 模式",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--path",
        default=".",
        help="扫描路径 (默认: 当前目录)"
    )

    parser.add_argument(
        "--output",
        help="输出报告到文件"
    )

    args = parser.parse_args()

    # 运行扫描
    enforcer = FailFastEnforcer(root_path=args.path)
    issues = enforcer.scan_all()

    # 打印报告
    enforcer.print_report()

    # 生成修复建议
    if args.output:
        recommendations = enforcer.generate_fix_recommendations()
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(recommendations)
        print(f"\n修复建议已保存到: {args.output}")

    # 退出码
    errors = [i for i in issues if i["severity"] == "error"]
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
