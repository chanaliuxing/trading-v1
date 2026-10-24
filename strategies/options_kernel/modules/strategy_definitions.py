from __future__ import annotations

from .definition import StrategyDefinition
from .direction_mapping import OptionNativeDirectionMappingPolicy, StockSignalOptionDirectionMappingPolicy
from .evidence_contract import OptionEvidenceContract
from .flatten_policy import TimeCutoffFlattenPolicy
from .generic_vwap_momentum_signal import GenericVwapMomentumSignalModule
from .ma_runner_position_policy import MARunnerPositionPolicy
from .profit_management_policy import MARunnerProfitManagementPolicy, QQQIntradayProfitManagementPolicy
from .qqq_intraday_option_position_policy import QQQIntradayOptionPositionPolicy
from .qqq_intraday_stock_signal import QQQIntradayStockSignalModule
from .registry import definition_registry
from .qqq_ma10_reject_signal import QQQMA10RejectSignalModule
from .risk_policy import MARunnerRiskPolicy, QQQIntradayUnderlyingRiskPolicy
from .spy_dual_signal import SPYDualSignalModule
from .standard_option_contract_policy import StandardOptionContractPolicy
from .standard_option_execution_policy import StandardOptionExecutionPolicy

OPTION_MA_EVIDENCE = OptionEvidenceContract(
    contract_id="option_ma_rollout_v1",
    replay_harness="options_kernel_harness",
    cutover_policy="replay_and_shadow_before_live",
)

QQQ_INTRADAY_OPTION_EVIDENCE = OptionEvidenceContract(
    contract_id="qqq_intraday_option_rollout_v1",
    replay_harness="options_kernel_harness",
    cutover_policy="qqq_state_shadow_acceptance_before_live",
    state_shadow_required=True,
)

GENERIC_INTRADAY_MOMENTUM_EVIDENCE = OptionEvidenceContract(
    contract_id="generic_intraday_momentum_rollout_v1",
    replay_harness="options_kernel_harness",
    cutover_policy="replay_and_shadow_before_live",
)


QQQ_MA10_REJECT_0324 = StrategyDefinition(
    definition_id="QQQ_MA10_REJECT_0324",
    strategy_family="qqq_ma_option",
    strategy_archetype="option_native_ma",
    underlying="QQQ",
    risk_symbol="OPT:QQQ:1DTE",
    signal_module=QQQMA10RejectSignalModule("ma10_reject_0324"),
    direction_mapping=OptionNativeDirectionMappingPolicy(),
    contract_policy=StandardOptionContractPolicy("standard_option_contract"),
    execution_policy=StandardOptionExecutionPolicy(policy_id="standard_option_execution", default_profile_id="qqq_option_ma_ext_v1"),
    position_policy=MARunnerPositionPolicy("ma_runner_position"),
    risk_policy=MARunnerRiskPolicy(),
    profit_management_policy=MARunnerProfitManagementPolicy(),
    flatten_policy=TimeCutoffFlattenPolicy(policy_id="ma_runner_session_flatten"),
    evidence_contract=OPTION_MA_EVIDENCE,
    audit_namespace="qqq_ma_option",
    ownership_scope="strategy_instance",
    extras={"replay_strategy_id": "ma10_reject_0324"},
)


QQQ_MA_CROSS_STACK_CONT_V2_DUAL = StrategyDefinition(
    definition_id="QQQ_MA_CROSS_STACK_CONT_V2_DUAL",
    strategy_family="qqq_ma_option",
    strategy_archetype="option_native_ma",
    underlying="QQQ",
    risk_symbol="OPT:QQQ:1DTE",
    signal_module=SPYDualSignalModule("ma_cross_stack_cont_v2_dual_exp"),
    direction_mapping=OptionNativeDirectionMappingPolicy(),
    contract_policy=StandardOptionContractPolicy("standard_option_contract"),
    execution_policy=StandardOptionExecutionPolicy(policy_id="standard_option_execution", default_profile_id="qqq_option_ma_ext_v1"),
    position_policy=MARunnerPositionPolicy("ma_runner_position"),
    risk_policy=MARunnerRiskPolicy(),
    profit_management_policy=MARunnerProfitManagementPolicy(),
    flatten_policy=TimeCutoffFlattenPolicy(policy_id="ma_runner_session_flatten"),
    evidence_contract=OPTION_MA_EVIDENCE,
    audit_namespace="qqq_ma_option",
    ownership_scope="strategy_instance",
    allowed_plugin_override_keys=("min_open_interest",),
    extras={"replay_strategy_id": "ma_cross_stack_cont_v2_dual_exp"},
)


SPY_MA_CROSS_STACK_CONT_V2_DUAL = StrategyDefinition(
    definition_id="SPY_MA_CROSS_STACK_CONT_V2_DUAL",
    strategy_family="spy_ma_option",
    strategy_archetype="option_native_ma",
    underlying="SPY",
    risk_symbol="OPT:SPY:1DTE",
    signal_module=SPYDualSignalModule("ma_cross_stack_cont_v2_dual_exp"),
    direction_mapping=OptionNativeDirectionMappingPolicy(),
    contract_policy=StandardOptionContractPolicy("standard_option_contract"),
    execution_policy=StandardOptionExecutionPolicy(policy_id="standard_option_execution", default_profile_id="spy_option_ma_ext_v1"),
    position_policy=MARunnerPositionPolicy("ma_runner_position"),
    risk_policy=MARunnerRiskPolicy(),
    profit_management_policy=MARunnerProfitManagementPolicy(),
    flatten_policy=TimeCutoffFlattenPolicy(policy_id="ma_runner_session_flatten"),
    evidence_contract=OPTION_MA_EVIDENCE,
    audit_namespace="spy_ma_option",
    ownership_scope="strategy_instance",
    extras={"replay_strategy_id": "ma_cross_stack_cont_v2_dual_exp"},
)

QQQ_INTRADAY_OPTION_PARITY_V1 = StrategyDefinition(
    definition_id="QQQ_INTRADAY_OPTION_PARITY_V1",
    strategy_family="qqq_intraday_option",
    strategy_archetype="stock_signal_option",
    underlying="QQQ",
    risk_symbol="OPT:QQQ:0DTE",
    signal_module=QQQIntradayStockSignalModule(),
    direction_mapping=StockSignalOptionDirectionMappingPolicy(),
    contract_policy=StandardOptionContractPolicy("standard_option_contract"),
    execution_policy=StandardOptionExecutionPolicy(
        policy_id="standard_option_execution",
        default_profile_id="qqq_intraday_option_parity_v1",
    ),
    position_policy=QQQIntradayOptionPositionPolicy("qqq_intraday_option_position_v1"),
    risk_policy=QQQIntradayUnderlyingRiskPolicy(),
    profit_management_policy=QQQIntradayProfitManagementPolicy(),
    flatten_policy=TimeCutoffFlattenPolicy(policy_id="qqq_intraday_eod_flatten"),
    evidence_contract=QQQ_INTRADAY_OPTION_EVIDENCE,
    audit_namespace="qqq_intraday_option",
    ownership_scope="strategy_instance",
    extras={"replay_strategy_id": "qqq_intraday_stock_signal_v2"},
)

SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1 = StrategyDefinition(
    definition_id="SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1",
    strategy_family="spy_intraday_momentum_option",
    strategy_archetype="generic_intraday_momentum",
    underlying="SPY",
    risk_symbol="OPT:SPY:0DTE",
    signal_module=GenericVwapMomentumSignalModule("generic_vwap_momentum_v1"),
    direction_mapping=StockSignalOptionDirectionMappingPolicy(),
    contract_policy=StandardOptionContractPolicy("standard_option_contract"),
    execution_policy=StandardOptionExecutionPolicy(
        policy_id="standard_option_execution",
        default_profile_id="spy_intraday_momentum_option_paper_v1",
    ),
    position_policy=QQQIntradayOptionPositionPolicy("spy_intraday_momentum_position_v1"),
    risk_policy=QQQIntradayUnderlyingRiskPolicy(policy_id="spy_intraday_momentum_underlying_stop"),
    profit_management_policy=QQQIntradayProfitManagementPolicy(policy_id="spy_intraday_momentum_profit_management"),
    flatten_policy=TimeCutoffFlattenPolicy(policy_id="spy_intraday_momentum_eod_flatten"),
    evidence_contract=GENERIC_INTRADAY_MOMENTUM_EVIDENCE,
    audit_namespace="spy_intraday_momentum_option",
    ownership_scope="strategy_instance",
    extras={"replay_strategy_id": "spy_intraday_momentum_option_paper_v1"},
)


definition_registry.register(QQQ_MA10_REJECT_0324)
definition_registry.register(QQQ_MA_CROSS_STACK_CONT_V2_DUAL)
definition_registry.register(SPY_MA_CROSS_STACK_CONT_V2_DUAL)
definition_registry.register(QQQ_INTRADAY_OPTION_PARITY_V1)
definition_registry.register(SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1)
