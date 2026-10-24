#!/usr/bin/env python3
"""
Configuration Sanity Check - 系统启动前配置校验

在系统启动前运行，检查所有关键配置项是否正确，避免运行时才发现配置错误。

Usage:
    python scripts/config_sanity_check.py [--config config.yml] [--strict]

Options:
    --config: 配置文件路径 (默认: config.yml)
    --strict: 严格模式，任何警告都视为错误

Exit Codes:
    0: 所有检查通过
    1: 发现错误（阻塞性）
    2: 发现警告（建议修复，但不阻塞）
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
import yaml
import json

try:
    from libs.logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)


class ConfigValidator:
    """配置验证器"""

    def __init__(self, config_path: str = "config.yml", strict: bool = False):
        self.config_path = Path(config_path)
        self.strict = strict
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.config: Optional[Dict[str, Any]] = None

    def validate_all(self) -> Tuple[bool, List[str], List[str]]:
        """
        运行所有验证检查

        Returns:
            (success, errors, warnings)
        """
        print(f"\n{'='*70}")
        print(f"配置文件校验: {self.config_path}")
        print(f"{'='*70}\n")

        # 1. 加载配置文件
        if not self._load_config():
            return False, self.errors, self.warnings

        # 2. 基础配置检查
        self._check_basic_config()

        # 3. 策略配置检查
        self._check_strategies_config()

        # 4. 数据库配置检查
        self._check_database_config()

        # 5. ML 配置检查
        self._check_ml_config()

        # 6. 风控配置检查
        self._check_risk_config()

        # 7. 环境变量检查
        self._check_environment_vars()

        # 8. 文件路径检查
        self._check_file_paths()

        # 打印结果
        self._print_results()

        # 返回结果
        success = len(self.errors) == 0
        if self.strict and len(self.warnings) > 0:
            success = False

        return success, self.errors, self.warnings

    def _load_config(self) -> bool:
        """加载配置文件"""
        if not self.config_path.exists():
            self.errors.append(f"配置文件不存在: {self.config_path}")
            return False

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
            print(f"✅ 配置文件加载成功: {self.config_path}")
            return True
        except Exception as e:
            self.errors.append(f"配置文件加载失败: {e}")
            return False

    def _check_basic_config(self):
        """检查基础配置"""
        print("\n[1/8] 检查基础配置...")

        # 检查环境模式
        env_mode = self.config.get("env_mode") or self.config.get("environment")
        if not env_mode:
            self.warnings.append("未设置 env_mode (推荐设置为 dev/test/production)")
        else:
            print(f"  ✓ ENV_MODE: {env_mode}")

        # 检查日志配置
        log_level = self.config.get("log_level") or self.config.get("logging", {}).get("level")
        if not log_level:
            self.warnings.append("未设置 log_level (推荐设置为 INFO/DEBUG)")
        else:
            print(f"  ✓ LOG_LEVEL: {log_level}")

    def _check_strategies_config(self):
        """检查策略配置"""
        print("\n[2/8] 检查策略配置...")

        strategies = self.config.get("strategies", [])
        if not strategies:
            self.warnings.append("未配置任何策略 (strategies 为空)")
            return

        print(f"  找到 {len(strategies)} 个策略配置")

        for idx, strategy in enumerate(strategies):
            strat_id = strategy.get("id", f"strategy_{idx}")

            # 检查必需字段
            if not strategy.get("module"):
                self.errors.append(f"策略 {strat_id}: 缺少 module 字段")
            if not strategy.get("class"):
                self.errors.append(f"策略 {strat_id}: 缺少 class 字段")

            # 检查策略启用状态
            enabled = strategy.get("enabled", False)
            if enabled:
                print(f"  ✓ {strat_id}: enabled={enabled}, module={strategy.get('module')}")

                # 检查参数配置
                params = strategy.get("params", {})
                if not params:
                    self.warnings.append(f"策略 {strat_id}: params 为空")

                # 检查 inference 配置（如果策略使用 ML）
                extras = params.get("extras", {})
                inf_cfg = extras.get("inference")
                if inf_cfg:
                    active_path = inf_cfg.get("active_path", "models/active")
                    if not Path(active_path).exists():
                        self.warnings.append(
                            f"策略 {strat_id}: inference.active_path 不存在: {active_path}"
                        )
            else:
                print(f"  - {strat_id}: disabled")

    def _check_database_config(self):
        """检查数据库配置"""
        print("\n[3/8] 检查数据库配置...")

        # ClickHouse
        ch_config = self.config.get("clickhouse") or self.config.get("databases", {}).get("clickhouse")
        if ch_config:
            host = ch_config.get("host", "localhost")
            port = ch_config.get("port", 9000)
            print(f"  ✓ ClickHouse: {host}:{port}")

            # 检查必需的表
            required_tables = [
                "trading.ml_eval_with_delta",  # 模型评估表
                "trading.features_1m_with_oof",  # 特征表
            ]
            # TODO: 实际连接 ClickHouse 检查表是否存在（可选）

        else:
            self.warnings.append("未配置 ClickHouse 连接")

        # Redis
        redis_config = self.config.get("redis") or self.config.get("databases", {}).get("redis")
        if redis_config:
            host = redis_config.get("host", "localhost")
            port = redis_config.get("port", 6379)
            print(f"  ✓ Redis: {host}:{port}")
        else:
            self.warnings.append("未配置 Redis 连接")

        # PostgreSQL
        pg_config = self.config.get("postgres") or self.config.get("databases", {}).get("postgres")
        if pg_config:
            host = pg_config.get("host", "localhost")
            port = pg_config.get("port", 5432)
            print(f"  ✓ PostgreSQL: {host}:{port}")

    def _check_ml_config(self):
        """检查 ML 配置"""
        print("\n[4/8] 检查 ML 配置...")

        # 检查模型目录
        model_dirs = [
            "models/active",
            "models/experiments",
        ]

        for model_dir in model_dirs:
            path = Path(model_dir)
            if path.exists():
                print(f"  ✓ 模型目录存在: {model_dir}")
            else:
                self.warnings.append(f"模型目录不存在: {model_dir}")

        # 检查 ENV_MODE（确保生产环境不使用 Mock/OOF 服务）
        import os
        env_mode = os.getenv("ENV_MODE", "dev").lower()
        if env_mode in {"production", "prod", "live"}:
            print(f"  ✓ 生产环境模式: {env_mode}")
            print(f"    → RealMLInferenceService 将被禁用")
            print(f"    → MockMLInferenceService 将被禁用")
        else:
            print(f"  ℹ 开发/测试模式: {env_mode}")

    def _check_risk_config(self):
        """检查风控配置"""
        print("\n[5/8] 检查风控配置...")

        # 检查 Guardian 配置
        guardian_config = self.config.get("guardian") or self.config.get("risk", {}).get("guardian")
        if guardian_config:
            max_dd = guardian_config.get("max_drawdown")
            max_position = guardian_config.get("max_position_size")
            print(f"  ✓ Guardian 配置存在")
            if max_dd:
                print(f"    max_drawdown: {max_dd}")
            if max_position:
                print(f"    max_position_size: {max_position}")
        else:
            self.warnings.append("未配置 Guardian 风控参数")

        # 检查 SafetyKernel 配置
        safety_config = self.config.get("safety_kernel") or self.config.get("risk", {}).get("safety_kernel")
        if safety_config:
            print(f"  ✓ SafetyKernel 配置存在")
        else:
            self.warnings.append("未配置 SafetyKernel 参数")

    def _check_environment_vars(self):
        """检查环境变量"""
        print("\n[6/8] 检查环境变量...")

        import os

        # 关键环境变量
        env_vars = {
            "ENV_MODE": "环境模式 (dev/test/production)",
            "IBKR_HOST": "IBKR 网关地址",
            "IBKR_PORT": "IBKR 网关端口",
        }

        for var, desc in env_vars.items():
            value = os.getenv(var)
            if value:
                # 敏感信息脱敏
                if "PASSWORD" in var or "SECRET" in var or "KEY" in var:
                    display_value = "***" + value[-4:] if len(value) > 4 else "***"
                else:
                    display_value = value
                print(f"  ✓ {var}: {display_value}")
            else:
                self.warnings.append(f"未设置环境变量: {var} ({desc})")

    def _check_file_paths(self):
        """检查关键文件路径"""
        print("\n[7/8] 检查关键文件路径...")

        # 检查 prompt library
        prompt_lib_paths = [
            "prompts/prompt_library.json",
            "config/prompts.yml",
        ]

        prompt_lib_exists = False
        for path_str in prompt_lib_paths:
            path = Path(path_str)
            if path.exists():
                print(f"  ✓ Prompt Library: {path_str}")
                prompt_lib_exists = True
                break

        if not prompt_lib_exists:
            self.warnings.append("未找到 Prompt Library 文件")

        # 检查 resume_state.json (Brain 状态持久化)
        resume_state_path = Path("resume_state.json")
        if resume_state_path.exists():
            print(f"  ✓ Brain Resume State: {resume_state_path}")
        else:
            print(f"  ℹ Brain Resume State 不存在 (首次运行时将创建)")

    def _print_results(self):
        """打印验证结果"""
        print(f"\n{'='*70}")
        print("验证结果汇总")
        print(f"{'='*70}\n")

        if self.errors:
            print(f"❌ 发现 {len(self.errors)} 个错误 (阻塞性):")
            for i, error in enumerate(self.errors, 1):
                print(f"  {i}. {error}")
            print()

        if self.warnings:
            print(f"⚠️  发现 {len(self.warnings)} 个警告 (建议修复):")
            for i, warning in enumerate(self.warnings, 1):
                print(f"  {i}. {warning}")
            print()

        if not self.errors and not self.warnings:
            print("✅ 所有检查通过！")
        elif not self.errors:
            print("⚠️  有警告但无阻塞性错误")
        else:
            print("❌ 发现阻塞性错误，请修复后再启动系统")

        print(f"{'='*70}\n")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Trading System Configuration Sanity Check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 检查默认配置文件
  python scripts/config_sanity_check.py

  # 检查指定配置文件
  python scripts/config_sanity_check.py --config config/production.yml

  # 严格模式（警告也视为错误）
  python scripts/config_sanity_check.py --strict
        """
    )

    parser.add_argument(
        "--config",
        default="config.yml",
        help="配置文件路径 (默认: config.yml)"
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：警告也视为错误"
    )

    args = parser.parse_args()

    # 运行验证
    validator = ConfigValidator(config_path=args.config, strict=args.strict)
    success, errors, warnings = validator.validate_all()

    # 退出码
    if success:
        sys.exit(0)
    elif errors:
        sys.exit(1)
    elif warnings and args.strict:
        sys.exit(1)
    else:
        sys.exit(2)  # 有警告但不是严格模式


if __name__ == "__main__":
    main()
