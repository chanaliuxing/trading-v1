"""Strategy plugins for the reusable option replay engine."""

from typing import Callable, Dict, List

from .ma10_reject_0324 import (
    MA10Reject0324Strategy,
    aggressive_entry_allowed,
    classify_signal_regime,
    detect_setup,
)
from .ma_cross_stack_continuation_exp import MACrossStackContinuationExperimentalStrategy
from .ma_cross_stack_continuation_v2_exp import (
    MACrossStackContinuationV2ClosePosGuardExperimentalStrategy,
    MACrossStackContinuationV2ClosePosVolGuardExperimentalStrategy,
    MACrossStackContinuationV2ClosePosVolSlopeGuardExperimentalStrategy,
    MACrossStackContinuationV2ExperimentalStrategy,
)
from .ma_cross_stack_continuation_v2_dual_exp import MACrossStackContinuationV2DualExperimentalStrategy
from .ma10_reject_0324_trend_continuation_exp import MA10Reject0324TrendContinuationExperimentalStrategy
from .ma10_reject_0324_trend_continuation_put_exp import MA10Reject0324TrendContinuationPutExperimentalStrategy
from .ma10_reject_0324_trend_continuation_put_loose_time_exp import (
    MA10Reject0324TrendContinuationPutLooseTimeExperimentalStrategy,
)
from .ma10_reject_0324_trend_continuation_put_runner_hardstop_exp import (
    MA10Reject0324TrendContinuationPutRunnerHardstopExperimentalStrategy,
)
from .ma10_reject_0324_trend_continuation_put_layered_softstop_exp import (
    MA10Reject0324TrendContinuationPutLayeredSoftStopExperimentalStrategy,
)
from .ma10_reject_0324_fresh_momentum_quality_exp import (
    MA10Reject0324FreshMomentumQualityExperimentalStrategy,
)
from .ma10_reject_0324_fresh_momentum_fakeout_exp import (
    MA10Reject0324FreshMomentumFakeoutExperimentalStrategy,
)
from .ma10_reject_0324_oracle0327_fallback_exp import (
    MA10Reject0324Oracle0327FallbackExperimentalStrategy,
)
from .ma10_reject_0324_oracle0327_fallback_pm_exp import (
    MA10Reject0324Oracle0327FallbackPMExperimentalStrategy,
)
from .ma10_reject_0324_layered_plus_oracle_fallback_exp import (
    MA10Reject0324LayeredPlusOracleFallbackExperimentalStrategy,
)
from .ma10_reject_0324_layered_plus_oracle_tc_call_opening_exp import (
    MA10Reject0324LayeredPlusOracleTCcallOpeningExperimentalStrategy,
)
from .ma_cross_retest_exp import MACrossRetestExperimentalStrategy
from .unified_ma_stack_score_exp import UnifiedMAStackScoreExperimentalStrategy
from .unified_ma_stack_score_v2_exp import UnifiedMAStackScoreV2ExperimentalStrategy
from .unified_ma_stack_score_v3_lowfreq_exp import UnifiedMAStackScoreV3LowFreqExperimentalStrategy
from .orb_opening_breakout import OpeningRangeBreakoutStrategy
from .vwap_reclaim_continuation import VWAPReclaimContinuationStrategy

STRATEGY_REGISTRY: Dict[str, Callable[[], object]] = {
    "ma10_reject_0324": MA10Reject0324Strategy,
    "ma_cross_stack_cont_exp": MACrossStackContinuationExperimentalStrategy,
    "ma_cross_stack_cont_v2_exp": MACrossStackContinuationV2ExperimentalStrategy,
    "ma_cross_stack_cont_v2_cp_guard_exp": MACrossStackContinuationV2ClosePosGuardExperimentalStrategy,
    "ma_cross_stack_cont_v2_cp_vol_guard_exp": MACrossStackContinuationV2ClosePosVolGuardExperimentalStrategy,
    "ma_cross_stack_cont_v2_cp_vol_slope_guard_exp": MACrossStackContinuationV2ClosePosVolSlopeGuardExperimentalStrategy,
    "ma_cross_stack_cont_v2_dual_exp": MACrossStackContinuationV2DualExperimentalStrategy,
    "ma10_reject_0324_trend_cont_exp": MA10Reject0324TrendContinuationExperimentalStrategy,
    "ma10_reject_0324_trend_cont_put_exp": MA10Reject0324TrendContinuationPutExperimentalStrategy,
    "ma10_reject_0324_trend_cont_put_loose_time_exp": MA10Reject0324TrendContinuationPutLooseTimeExperimentalStrategy,
    "ma10_reject_0324_trend_cont_put_runner_hardstop_exp": MA10Reject0324TrendContinuationPutRunnerHardstopExperimentalStrategy,
    "ma10_reject_0324_trend_cont_put_layered_softstop_exp": MA10Reject0324TrendContinuationPutLayeredSoftStopExperimentalStrategy,
    "ma10_reject_0324_fresh_momentum_quality_exp": MA10Reject0324FreshMomentumQualityExperimentalStrategy,
    "ma10_reject_0324_fresh_momentum_fakeout_exp": MA10Reject0324FreshMomentumFakeoutExperimentalStrategy,
    "ma10_reject_0324_oracle0327_fallback_exp": MA10Reject0324Oracle0327FallbackExperimentalStrategy,
    "ma10_reject_0324_oracle0327_fallback_pm_exp": MA10Reject0324Oracle0327FallbackPMExperimentalStrategy,
    "ma10_reject_0324_layered_plus_oracle_fallback_exp": MA10Reject0324LayeredPlusOracleFallbackExperimentalStrategy,
    "ma10_reject_0324_layered_plus_oracle_tc_call_opening_exp": MA10Reject0324LayeredPlusOracleTCcallOpeningExperimentalStrategy,
    "ma_cross_retest_exp": MACrossRetestExperimentalStrategy,
    "unified_ma_stack_score_exp": UnifiedMAStackScoreExperimentalStrategy,
    "unified_ma_stack_score_v2_exp": UnifiedMAStackScoreV2ExperimentalStrategy,
    "unified_ma_stack_score_v3_lowfreq_exp": UnifiedMAStackScoreV3LowFreqExperimentalStrategy,
    "orb_opening_breakout": OpeningRangeBreakoutStrategy,
    "orb": OpeningRangeBreakoutStrategy,
    "vwap_reclaim_continuation": VWAPReclaimContinuationStrategy,
    "vwap": VWAPReclaimContinuationStrategy,
}


def list_strategies() -> List[str]:
    return sorted(STRATEGY_REGISTRY.keys())


def get_strategy(name: str):
    key = (name or "").strip().lower()
    if key not in STRATEGY_REGISTRY:
        raise KeyError(f"unknown strategy: {name}")
    return STRATEGY_REGISTRY[key]()


__all__ = [
    "MA10Reject0324Strategy",
    "MACrossStackContinuationExperimentalStrategy",
    "MACrossStackContinuationV2ExperimentalStrategy",
    "MACrossStackContinuationV2ClosePosGuardExperimentalStrategy",
    "MACrossStackContinuationV2ClosePosVolGuardExperimentalStrategy",
    "MACrossStackContinuationV2ClosePosVolSlopeGuardExperimentalStrategy",
    "MACrossStackContinuationV2DualExperimentalStrategy",
    "MA10Reject0324TrendContinuationExperimentalStrategy",
    "MA10Reject0324TrendContinuationPutExperimentalStrategy",
    "MA10Reject0324TrendContinuationPutLooseTimeExperimentalStrategy",
    "MA10Reject0324TrendContinuationPutRunnerHardstopExperimentalStrategy",
    "MA10Reject0324TrendContinuationPutLayeredSoftStopExperimentalStrategy",
    "MA10Reject0324FreshMomentumQualityExperimentalStrategy",
    "MA10Reject0324FreshMomentumFakeoutExperimentalStrategy",
    "MA10Reject0324Oracle0327FallbackExperimentalStrategy",
    "MA10Reject0324Oracle0327FallbackPMExperimentalStrategy",
    "MA10Reject0324LayeredPlusOracleFallbackExperimentalStrategy",
    "MA10Reject0324LayeredPlusOracleTCcallOpeningExperimentalStrategy",
    "MACrossRetestExperimentalStrategy",
    "UnifiedMAStackScoreExperimentalStrategy",
    "UnifiedMAStackScoreV2ExperimentalStrategy",
    "UnifiedMAStackScoreV3LowFreqExperimentalStrategy",
    "OpeningRangeBreakoutStrategy",
    "VWAPReclaimContinuationStrategy",
    "STRATEGY_REGISTRY",
    "aggressive_entry_allowed",
    "classify_signal_regime",
    "detect_setup",
    "get_strategy",
    "list_strategies",
]
