from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml


DEFAULT_RETIRED_OPTION_STRATEGY_IDS = frozenset(
    {
        "qqq_0dte_opt_live_pure_001",
        "qqq_1dte_ma10_reject_0324_v2_001",
    }
)


def _is_option_strategy_row(row: Dict[str, Any]) -> bool:
    strategy_id = str(row.get("id") or "").lower()
    module = str(row.get("module") or "").lower()
    klass = str(row.get("class") or "").lower()
    extras = (((row.get("params") or {}).get("extras")) or {})
    risk_symbol = str((extras or {}).get("risk_symbol") or "").upper()
    return any(
        (
            "option" in strategy_id,
            "option" in module,
            "option" in klass,
            "0dte_opt" in strategy_id,
            "1dte_ma10_reject" in strategy_id,
            risk_symbol.startswith("OPT:"),
        )
    )


def _platform_from_row(row: Dict[str, Any]) -> str:
    module = str(row.get("module") or "")
    extras = (((row.get("params") or {}).get("extras")) or {})
    kernel_mode = str((extras or {}).get("kernel_mode") or "").strip().lower()
    if "options_kernel" in module or kernel_mode in {"plugin", "pure_plugin", "hybrid"}:
        return "plugin_kernel"
    if "strategies.options." in module:
        return "standalone_option_family"
    return "legacy_option_runtime"


def _definition_id_from_row(row: Dict[str, Any]) -> str:
    extras = (((row.get("params") or {}).get("extras")) or {})
    return str((extras or {}).get("definition_id") or "").strip()


def build_option_strategy_inventory(
    config_payload: Dict[str, Any],
    *,
    retired_ids: Iterable[str] = DEFAULT_RETIRED_OPTION_STRATEGY_IDS,
) -> Dict[str, Any]:
    retired = {str(item).strip() for item in retired_ids if str(item).strip()}
    strategies = [row for row in list(config_payload.get("strategies") or []) if isinstance(row, dict)]
    option_rows: List[Dict[str, Any]] = []
    active_ids: List[str] = []
    disabled_ids: List[str] = []
    retired_hits: List[str] = []

    for row in strategies:
        if not _is_option_strategy_row(row):
            continue
        strategy_id = str(row.get("id") or "").strip()
        enabled = bool(row.get("enabled", False))
        status = "active"
        if strategy_id in retired:
            status = "retired"
            retired_hits.append(strategy_id)
        elif not enabled:
            status = "disabled"
            disabled_ids.append(strategy_id)
        else:
            active_ids.append(strategy_id)
        extras = (((row.get("params") or {}).get("extras")) or {})
        option_rows.append(
            {
                "strategy_id": strategy_id,
                "enabled": enabled,
                "status": status,
                "module": str(row.get("module") or ""),
                "class": str(row.get("class") or ""),
                "underlying": str((extras or {}).get("underlying") or ""),
                "risk_symbol": str((extras or {}).get("risk_symbol") or ""),
                "kernel_mode": str((extras or {}).get("kernel_mode") or ""),
                "definition_id": _definition_id_from_row(row),
                "platform": _platform_from_row(row),
                "shared_cap_group_id": str((extras or {}).get("shared_cap_group_id") or ""),
                "shared_cap_group_strategy_ids": list((extras or {}).get("shared_cap_group_strategy_ids") or []),
            }
        )

    option_rows.sort(key=lambda item: item["strategy_id"])
    return {
        "summary": {
            "option_strategy_count": len(option_rows),
            "active_count": len(active_ids),
            "disabled_count": len(disabled_ids),
            "retired_count": len(retired_hits),
        },
        "active_strategy_ids": sorted(active_ids),
        "disabled_strategy_ids": sorted(disabled_ids),
        "retired_strategy_ids": sorted(retired_hits),
        "strategies": option_rows,
    }


def build_option_strategy_inventory_from_config(
    config_path: str | Path,
    *,
    retired_ids: Iterable[str] = DEFAULT_RETIRED_OPTION_STRATEGY_IDS,
) -> Dict[str, Any]:
    payload = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    return build_option_strategy_inventory(payload, retired_ids=retired_ids)
