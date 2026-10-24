"""
Consistency Validator - Cross-configuration consistency checks

This validator checks:
- Strategy symbols vs universe consistency
- Duplicate strategy IDs
- HPO gates parameter consistency
- Features table existence (via ClickHouse)
- Parameter range reasonableness

Design: Semantic validation beyond schema
"""

from typing import Dict, List, Any, Optional


class ConsistencyValidator:
    """跨配置一致性验证器"""

    def __init__(self, clickhouse_client: Optional[Any] = None):
        """
        初始化一致性验证器

        Args:
            clickhouse_client: ClickHouse 客户端（可选，用于表存在性检查）
        """
        self.ch_client = clickhouse_client

    def validate_consistency(
        self,
        config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        验证配置间的一致性

        Args:
            config: 主配置字典

        Returns:
            {"ok": bool, "issues": List[str], "warnings": List[str]}
        """
        issues = []
        warnings = []

        # 1. 检查策略 symbols 与 universe 一致性
        issues.extend(self._validate_symbol_universe_consistency(config))

        # 2. 检查重复的策略 ID
        issues.extend(self._validate_unique_strategy_ids(config))

        # 3. 检查 HPO 门控参数一致性
        issues.extend(self._validate_hpo_gates_consistency(config))

        # 4. 检查风险参数合理性
        warnings.extend(self._validate_risk_parameter_reasonableness(config))

        # 5. 检查执行参数一致性
        warnings.extend(self._validate_execution_consistency(config))

        return {
            "ok": len(issues) == 0,
            "issues": issues,
            "warnings": warnings
        }

    def _validate_symbol_universe_consistency(
        self,
        config: Dict[str, Any]
    ) -> List[str]:
        """验证策略使用的 symbols 是否在 universe 中"""
        issues = []

        universe = set(config.get('universe', []))
        if not universe:
            return issues  # universe 为空会在 SchemaValidator 中捕获

        strategies = config.get('strategies', [])

        for strat in strategies:
            strat_id = strat.get('id', 'unknown')
            extras = strat.get('params', {}).get('extras', {})

            # 获取策略使用的 symbols
            symbols = extras.get('symbols', [])

            # 特殊处理：tsla_v7_2_config.symbol
            tsla_config = extras.get('tsla_v7_2_config', {})
            if tsla_config:
                tsla_symbol = tsla_config.get('symbol')
                if tsla_symbol and tsla_symbol not in symbols:
                    symbols.append(tsla_symbol)

            # 检查每个 symbol 是否在 universe 中
            for sym in symbols:
                if sym not in universe:
                    issues.append(
                        f"symbol_not_in_universe: '{sym}' used by strategy '{strat_id}' "
                        f"is not in universe {sorted(universe)}"
                    )

        return issues

    def _validate_unique_strategy_ids(
        self,
        config: Dict[str, Any]
    ) -> List[str]:
        """检查策略 ID 是否唯一"""
        issues = []

        strategies = config.get('strategies', [])
        strat_ids = [s.get('id') for s in strategies]

        # 找出重复的 ID
        seen = set()
        duplicates = []

        for sid in strat_ids:
            if sid in seen:
                duplicates.append(sid)
            seen.add(sid)

        if duplicates:
            issues.append(
                f"duplicate_strategy_ids: {sorted(set(duplicates))}"
            )

        return issues

    def _validate_hpo_gates_consistency(
        self,
        config: Dict[str, Any]
    ) -> List[str]:
        """验证 HPO 门控参数的一致性"""
        issues = []

        hpo_gates = config.get('policies', {}).get('hpo', {}).get('success_gates', {})

        if not hpo_gates:
            return issues  # HPO gates 是可选的

        min_trials = hpo_gates.get('min_trials', 0)
        min_unique = hpo_gates.get('min_unique_params', 0)

        # min_unique_params 应该 <= min_trials
        if min_unique > min_trials:
            issues.append(
                f"hpo_gates_inconsistent: min_unique_params ({min_unique}) "
                f"cannot be greater than min_trials ({min_trials})"
            )

        # convergence_score_max 应该在 [0, 1] 范围内
        convergence_max = hpo_gates.get('convergence_score_max')
        if convergence_max is not None:
            if not isinstance(convergence_max, (int, float)):
                issues.append(
                    f"hpo_gates.convergence_score_max: must be number, "
                    f"got {type(convergence_max).__name__}"
                )
            elif convergence_max < 0 or convergence_max > 1:
                issues.append(
                    f"hpo_gates.convergence_score_max: {convergence_max} "
                    f"outside valid range [0, 1]"
                )

        # IC gates（如果启用）
        min_ic = hpo_gates.get('min_ic')
        if min_ic is not None:
            if not isinstance(min_ic, (int, float)):
                issues.append(
                    f"hpo_gates.min_ic: must be number, got {type(min_ic).__name__}"
                )
            elif min_ic < -1 or min_ic > 1:
                issues.append(
                    f"hpo_gates.min_ic: {min_ic} outside valid range [-1, 1]"
                )

        return issues

    def _validate_risk_parameter_reasonableness(
        self,
        config: Dict[str, Any]
    ) -> List[str]:
        """验证风险参数的合理性（警告）"""
        warnings = []

        risk = config.get('risk', {})

        # 检查 per_trade_risk_pct 是否过高
        per_trade_risk = risk.get('per_trade_risk_pct')
        if per_trade_risk is not None and isinstance(per_trade_risk, (int, float)):
            if per_trade_risk > 0.02:  # 2%
                warnings.append(
                    f"risk.per_trade_risk_pct: {per_trade_risk} (>2%) "
                    f"is unusually high, consider reducing"
                )

        # 检查 max_concurrent_positions 是否合理
        max_positions = risk.get('max_concurrent_positions')
        universe_size = len(config.get('universe', []))

        if max_positions is not None and universe_size > 0:
            if max_positions > universe_size:
                warnings.append(
                    f"risk.max_concurrent_positions: {max_positions} exceeds "
                    f"universe size {universe_size}"
                )

        # 检查 daily_trading_capital 是否过小
        capital = risk.get('daily_trading_capital')
        if capital is not None and isinstance(capital, (int, float)):
            if capital < 10000:
                warnings.append(
                    f"risk.daily_trading_capital: {capital} is very low, "
                    f"may limit strategy effectiveness"
                )

        return warnings

    def _validate_execution_consistency(
        self,
        config: Dict[str, Any]
    ) -> List[str]:
        """验证执行参数的一致性（警告）"""
        warnings = []

        execution = config.get('execution', {})
        mode = config.get('mode', 'paper')

        # Paper mode 检查
        if mode == 'paper':
            if not execution.get('enabled', True):
                warnings.append(
                    "execution.enabled=false in paper mode - "
                    "no orders will be sent (is this intentional?)"
                )

        # Live mode 检查
        if mode == 'live':
            if not execution.get('enabled', False):
                warnings.append(
                    "execution.enabled=false in live mode - "
                    "system will run in dry-run mode"
                )

            # 检查是否设置了账户
            account = execution.get('default_account', '')
            if not account:
                warnings.append(
                    "execution.default_account not set in live mode - "
                    "this may cause execution failures"
                )

        return warnings

    def validate_features_table_exists(
        self,
        table_name: str
    ) -> Dict[str, Any]:
        """
        验证特征表是否存在（需要 ClickHouse 连接）

        Args:
            table_name: 表名（如 'trading.features_1m'）

        Returns:
            {"ok": bool, "error": Optional[str]}
        """
        if self.ch_client is None:
            return {
                "ok": True,  # 跳过检查
                "error": "ClickHouse client not provided, skipping table check"
            }

        try:
            # 解析表名
            if '.' in table_name:
                database, table = table_name.split('.', 1)
            else:
                database, table = 'default', table_name

            # 查询表是否存在
            query = """
                SELECT count()
                FROM system.tables
                WHERE database = %(database)s AND name = %(table)s
            """

            result = self.ch_client.execute(
                query,
                {'database': database, 'table': table}
            )

            # result is a list of tuples: [(count,)]
            exists = result and len(result) > 0 and result[0][0] > 0

            if not exists:
                return {
                    "ok": False,
                    "error": f"features_table '{table_name}' does not exist in ClickHouse"
                }

            return {"ok": True, "error": None}

        except Exception as e:
            return {
                "ok": False,
                "error": f"Failed to check features_table existence: {e}"
            }

    def validate_hpo_consistency(
        self,
        hpo_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        验证 HPO 配置的内部一致性

        Args:
            hpo_config: HPO 配置字典

        Returns:
            {"ok": bool, "issues": List[str]}
        """
        issues = []

        hpo = hpo_config.get('hpo', {})

        # 1. 检查 symbols 与 features_table 的一致性
        symbols = hpo.get('symbols', [])
        features_table = hpo.get('features_table', '')

        if not symbols:
            issues.append("hpo.symbols: at least one symbol required")

        # 2. 检查 fold_generator 时间范围
        fold_gen = hpo.get('fold_generator', {})
        if fold_gen:
            is_days = fold_gen.get('is_days', 0)
            oos_days = fold_gen.get('oos_days', 0)
            gap_days = fold_gen.get('gap_days', 0)

            if is_days <= 0:
                issues.append(f"fold_generator.is_days: {is_days} must be > 0")

            if oos_days <= 0:
                issues.append(f"fold_generator.oos_days: {oos_days} must be > 0")

            if gap_days < 0:
                issues.append(f"fold_generator.gap_days: {gap_days} must be >= 0")

            # is_days 应该 >= oos_days（通常）
            if is_days > 0 and oos_days > 0 and is_days < oos_days:
                issues.append(
                    f"fold_generator: is_days ({is_days}) < oos_days ({oos_days}) "
                    f"is unusual, verify this is intentional"
                )

        # 3. 检查 objective 的一致性
        objective = hpo.get('objective', {})
        metric = objective.get('metric', '')
        maximize = objective.get('maximize', True)

        # 某些指标应该最大化，某些应该最小化
        should_maximize = {
            'sharpe': True,
            'sortino': True,
            'calmar': True,
            'omega': True,
            'profit_factor': True,
            'win_rate': True,
            'total_return': True,
            'mdd': False,  # Max Drawdown 应该最小化
            'volatility': False,
        }

        if metric.lower() in should_maximize:
            expected = should_maximize[metric.lower()]
            if maximize != expected:
                issues.append(
                    f"objective: metric '{metric}' should have maximize={expected}, "
                    f"got maximize={maximize}"
                )

        return {
            "ok": len(issues) == 0,
            "issues": issues
        }
