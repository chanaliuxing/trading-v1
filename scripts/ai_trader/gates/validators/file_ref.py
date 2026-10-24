"""
File Reference Validator - Validates existence of referenced files

This validator checks:
- LKG (Last Known Good) file references
- Premarket calibration parameter files
- Per-symbol configuration files
- Model files and checkpoints

Design: Proactive file existence checking before runtime
"""

from typing import Dict, List, Any
from pathlib import Path


class FileRefValidator:
    """文件引用验证器"""

    def validate_all_refs(
        self,
        main_config: Dict[str, Any],
        strategy_configs_dir: str = "strategies/configs"
    ) -> Dict[str, Any]:
        """
        验证所有文件引用的存在性

        Args:
            main_config: 主配置字典
            strategy_configs_dir: 策略配置文件目录

        Returns:
            {"ok": bool, "missing": List[str], "warnings": List[str]}
        """
        missing = []
        warnings = []

        # 遍历所有策略
        strategies = main_config.get('strategies', [])

        for strat in strategies:
            strat_id = strat.get('id', 'unknown')
            enabled = strat.get('enabled', True)  # 检查策略是否启用
            strategy_class = strat.get('class', '')
            extras = strat.get('params', {}).get('extras', {})

            # 1. 检查 TSLASimplifiedV7_2 策略的配置
            tsla_config = extras.get('tsla_v7_2_config', {})
            if tsla_config:
                # 禁用的策略仅警告，不报错
                result_list = self._validate_tsla_v7_2_refs(tsla_config, strat_id)
                if enabled:
                    missing.extend(result_list)
                else:
                    warnings.extend(
                        [msg.replace('lkg_file_not_found', 'lkg_file_not_found_disabled_strategy')
                         for msg in result_list]
                    )

            # 2. 检查 EnhancedModelSignalStrategy 的配置 (明确检查策略类名)
            if strategy_class == 'EnhancedModelSignalStrategy':
                warnings.extend(
                    self._validate_enhanced_ml_refs(extras, strat_id)
                )

            # 3. 检查通用文件引用（仅启用的策略）
            if enabled:
                missing.extend(
                    self._validate_generic_file_refs(extras, strat_id)
                )

        return {
            "ok": len(missing) == 0,
            "missing": missing,
            "warnings": warnings
        }

    def _validate_tsla_v7_2_refs(
        self,
        tsla_config: Dict[str, Any],
        strat_id: str
    ) -> List[str]:
        """验证 TSLASimplifiedV7_2 策略的文件引用"""
        missing = []

        # 1. 检查 LKG 文件
        lkg = tsla_config.get('lkg', {})
        if lkg.get('enabled', False):
            lkg_file = lkg.get('file')
            if lkg_file:
                if not Path(lkg_file).exists():
                    missing.append(
                        f"lkg_file_not_found: {lkg_file} "
                        f"(strategy: {strat_id})"
                    )
            else:
                missing.append(
                    f"lkg_enabled_but_no_file: strategy {strat_id} has "
                    f"lkg.enabled=true but no lkg.file specified"
                )

        # 2. 检查 premarket_calibration.param_file
        pm_calib = tsla_config.get('premarket_calibration', {})
        param_file = pm_calib.get('param_file')
        if param_file:
            if not Path(param_file).exists():
                missing.append(
                    f"param_file_not_found: {param_file} "
                    f"(strategy: {strat_id})"
                )

        return missing

    def _validate_enhanced_ml_refs(
        self,
        extras: Dict[str, Any],
        strat_id: str
    ) -> List[str]:
        """验证 EnhancedModelSignalStrategy 的文件引用（仅警告）"""
        warnings = []

        config_dir = extras.get('config_dir', 'strategies/configs')
        config_prefix = extras.get('config_prefix', 'enhanced_ml')
        symbols = extras.get('symbols', [])

        # 检查 per-symbol 配置文件
        for symbol in symbols:
            config_file = Path(config_dir) / f"{config_prefix}_{symbol}.json"

            # 这些文件是可选的（策略会使用默认值），只警告
            if not config_file.exists():
                warnings.append(
                    f"symbol_config_missing: {config_file} "
                    f"(strategy: {strat_id}, will use defaults)"
                )

        # 检查 RL 模型文件（如果启用）
        rl_config = extras.get('rl', {})
        if rl_config.get('enabled', False):
            model_path = rl_config.get('model_path')
            if model_path:
                if not Path(model_path).exists():
                    warnings.append(
                        f"rl_model_not_found: {model_path} "
                        f"(strategy: {strat_id}, RL will be disabled)"
                    )

        return warnings

    def _validate_generic_file_refs(
        self,
        extras: Dict[str, Any],
        strat_id: str
    ) -> List[str]:
        """验证通用文件引用"""
        missing = []

        # 递归检查 extras 中的所有路径引用
        def check_dict(d: Any, prefix: str = ""):
            if not isinstance(d, dict):
                return

            for key, value in d.items():
                full_key = f"{prefix}.{key}" if prefix else key

                # 检查明显的文件路径字段
                if any(keyword in key.lower() for keyword in ['file', 'path', 'config_file']):
                    if isinstance(value, str):
                        # 跳过非文件路径（URL, 特殊值等）
                        if value.startswith(('http://', 'https://', 'redis://', 'kafka://')):
                            continue
                        if value in ('', 'auto', 'none', 'null'):
                            continue

                        # 检查文件是否存在
                        if not Path(value).exists():
                            missing.append(
                                f"file_not_found: {full_key}={value} "
                                f"(strategy: {strat_id})"
                            )

                # 递归检查嵌套字典
                elif isinstance(value, dict):
                    check_dict(value, full_key)

        check_dict(extras)

        return missing

    def validate_hpo_file_refs(
        self,
        hpo_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        验证 HPO 配置中的文件引用

        Args:
            hpo_config: HPO 配置字典

        Returns:
            {"ok": bool, "missing": List[str]}
        """
        missing = []

        hpo = hpo_config.get('hpo', {})

        # 检查 features_table 是否存在（需要 ClickHouse 查询）
        # 这里只做基本检查，实际存在性由 ConsistencyValidator 检查
        features_table = hpo.get('features_table')
        if not features_table:
            missing.append("hpo.features_table: required field missing")

        # 检查 strategy_path 是否可导入（基本格式检查）
        strategy_path = hpo.get('strategy_path')
        if strategy_path:
            # 验证格式: module.path.ClassName
            if '.' not in strategy_path:
                missing.append(
                    f"hpo.strategy_path: invalid format '{strategy_path}' "
                    f"(expected: module.path.ClassName)"
                )
        else:
            missing.append("hpo.strategy_path: required field missing")

        # 检查 checkpoint/storage 路径
        optuna_cfg = hpo_config.get('optuna', {})
        storage = optuna_cfg.get('storage', '')

        if storage and storage.startswith('sqlite:///'):
            db_file = storage.replace('sqlite:///', '')
            db_path = Path(db_file).parent

            # 检查目录是否存在（文件可以不存在，Optuna会创建）
            if db_path != Path('.') and not db_path.exists():
                missing.append(
                    f"optuna.storage: parent directory does not exist: {db_path}"
                )

        return {
            "ok": len(missing) == 0,
            "missing": missing
        }
