from __future__ import annotations

from typing import Any, Dict

from .types import ExecutionProfile


def build_execution_profile_from_extras(
    *,
    profile_id: str,
    market_data_profile_id: str,
    extras: Dict[str, Any] | None,
) -> ExecutionProfile:
    cfg = dict(extras or {})
    contracts = int(cfg.get("contracts", cfg.get("contracts_per_trade", 4)) or 4)
    return ExecutionProfile(
        profile_id=str(profile_id or "").strip() or "option_exec_v1",
        market_data_profile_id=str(market_data_profile_id or "").strip() or "standard_close_extended_sma",
        timeout_bars=max(
            1,
            int(
                cfg.get(
                    "continuation_pending_entry_timeout_bars",
                    cfg.get("pending_entry_timeout_bars", 2),
                )
                or 2
            ),
        ),
        dynamic_reprice_to_ask=bool(cfg.get("continuation_dynamic_reprice_to_ask", False)),
        max_premium_expansion_pct=float(cfg.get("continuation_max_premium_expansion_pct", 0.0) or 0.0),
        partial_take_profit_contracts=max(0, min(int(contracts), 2 if int(contracts) >= 2 else int(contracts))),
        stop_rule=str(cfg.get("stop_rule") or "structure_stop").strip() or "structure_stop",
        time_stop_soft_bars=max(0, int(cfg.get("time_stop_soft_bars", 5) or 5)),
        time_stop_hard_bars=max(0, int(cfg.get("time_stop_hard_bars", 10) or 10)),
        payload={
            "contracts": int(contracts),
            "force_flatten_time": str(cfg.get("force_flatten_time") or ""),
            "spread_max_pct": float(cfg.get("spread_max_pct", 0.12) or 0.12),
        },
    )
