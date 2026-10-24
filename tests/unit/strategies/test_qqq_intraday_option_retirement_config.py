from pathlib import Path

import yaml


CONFIG_PATH = Path("./config.yml")
LEGACY_IDS = {"qqq_0dte_opt_live_pure_001", "qqq_1dte_ma10_reject_0324_v2_001"}


def _load_cfg() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_legacy_qqq_option_paths_remain_disabled() -> None:
    cfg = _load_cfg()
    strategies = list(cfg.get("strategies") or [])
    by_id = {str(row.get("id")): row for row in strategies if isinstance(row, dict)}
    for strategy_id in LEGACY_IDS:
        assert strategy_id in by_id
        assert bool(by_id[strategy_id].get("enabled", True)) is False


def test_legacy_qqq_option_paths_not_in_telegram_allowlist() -> None:
    cfg = _load_cfg()
    allowlist = set(
        str(v)
        for v in (
            (((cfg.get("notifications") or {}).get("telegram") or {}).get("strategy_allowlist") or [])
        )
    )
    assert allowlist.isdisjoint(LEGACY_IDS)


def test_enabled_qqq_option_strategies_do_not_reference_legacy_shared_cap_ids() -> None:
    cfg = _load_cfg()
    for row in list(cfg.get("strategies") or []):
        if not isinstance(row, dict):
            continue
        if not bool(row.get("enabled", False)):
            continue
        strategy_id = str(row.get("id") or "")
        extras = (((row.get("params") or {}).get("extras")) or {})
        if not isinstance(extras, dict):
            continue
        group_ids = set(str(v) for v in (extras.get("shared_cap_group_strategy_ids") or []))
        if "qqq" not in strategy_id:
            continue
        assert group_ids.isdisjoint(LEGACY_IDS), f"{strategy_id} still references retired ids: {group_ids & LEGACY_IDS}"
