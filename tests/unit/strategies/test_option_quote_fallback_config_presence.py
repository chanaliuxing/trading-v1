from __future__ import annotations

from pathlib import Path

import yaml

STANDARD_OPTION_CONTRACT_DEFINITION_IDS = {
    "QQQ_MA10_REJECT_0324",
    "QQQ_MA_CROSS_STACK_CONT_V2_DUAL",
    "SPY_MA_CROSS_STACK_CONT_V2_DUAL",
    "QQQ_INTRADAY_OPTION_PARITY_V1",
    "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1",
}

def _load_config() -> dict:
    cfg_path = Path("./config.yml")
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def test_definition_driven_option_strategies_have_explicit_quote_fallback_flags():
    cfg = _load_config()
    checked = 0
    for strategy in cfg.get("strategies", []):
        if not bool(strategy.get("enabled", False)):
            continue
        extras = ((strategy.get("params") or {}).get("extras") or {})
        definition_id = str(extras.get("definition_id") or "").strip()
        if not definition_id:
            continue
        if definition_id not in STANDARD_OPTION_CONTRACT_DEFINITION_IDS:
            continue
        checked += 1
        assert "entry_quote_allow_ch_fallback" in extras, f"{strategy.get('id')} missing entry_quote_allow_ch_fallback"
        assert "mark_quote_allow_ch_fallback" in extras, f"{strategy.get('id')} missing mark_quote_allow_ch_fallback"
        assert isinstance(extras["entry_quote_allow_ch_fallback"], bool), f"{strategy.get('id')} entry fallback must be bool"
        assert isinstance(extras["mark_quote_allow_ch_fallback"], bool), f"{strategy.get('id')} mark fallback must be bool"
    assert checked > 0
