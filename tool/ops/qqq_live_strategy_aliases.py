from __future__ import annotations

from collections.abc import Iterable

LEGACY_QQQ_LIVE_STRATEGY_ID = "qqq_intraday_001"
OPTION_QQQ_LIVE_STRATEGY_ID = "qqq_intraday_option_v1_001"
QQQ_LIVE_STRATEGY_ALIASES = (
    LEGACY_QQQ_LIVE_STRATEGY_ID,
    OPTION_QQQ_LIVE_STRATEGY_ID,
)


def matched_qqq_live_strategy_ids(strategy_ids: Iterable[str]) -> list[str]:
    present = {str(item).strip() for item in strategy_ids if str(item).strip()}
    return [strategy_id for strategy_id in QQQ_LIVE_STRATEGY_ALIASES if strategy_id in present]
