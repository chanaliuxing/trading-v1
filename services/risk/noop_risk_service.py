"""
No-op risk service for controlled test runs.

When enabled, this service always approves execution requests and performs no
capital reservation/release. This is intended for paper-trading smoke tests.
"""
from __future__ import annotations

import time
from typing import Any, Dict

from libs.logger import get_logger
from services.risk.risk_service import RiskDecision

logger = get_logger(__name__)


class NoopRiskService:
    """Drop-in risk service replacement that always approves."""

    async def approve(self, envelope: Dict[str, Any], idempotency_key: str) -> RiskDecision:
        decision_id = str(envelope.get("decision_id", ""))
        symbol = str(envelope.get("symbol", ""))
        strategy_id = str(envelope.get("strategy_id", ""))

        logger.warning(
            "risk_bypass_approved",
            decision_id=decision_id,
            strategy_id=strategy_id,
            symbol=symbol,
            idempotency_key=idempotency_key,
        )

        return RiskDecision(
            decision_id=decision_id,
            approved=True,
            reason="risk_bypass_enabled",
            hits=[],
            reserved_amount=None,
            strategy_cap_used=None,
            ts_us=int(time.time() * 1_000_000),
        )

    async def release(self, idempotency_key: str) -> bool:
        logger.info("risk_bypass_release_noop", idempotency_key=idempotency_key)
        return True

