import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.compare_index_super_win_shadow import compare_shadow


def test_compare_shadow_passes_when_same_symbol_is_blocked_by_foreign_owner():
    qqq_obs = {
        "ready": True,
        "strategy_state": {
            "is_traded": True,
            "side": "LONG",
            "exec_symbol": "AAPL",
        },
        "missing_required": [],
    }
    index_diag = {
        "entry_enabled_by_symbol": {"AAPL": True},
        "trader_view_by_symbol": {
            "AAPL": {
                "final_state": "blocked",
                "which_gate_blocked": "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed",
                "broker_open_order_conflict": False,
                "broker_position_conflict": False,
                "foreign_owner_strategy_id": "qqq_intraday_001",
                "last_preview": {"status": "OWNERSHIP_CONFLICT"},
            }
        },
        "conflict_report_by_symbol": {
            "AAPL": {
                "foreign_owner_strategy_id": "qqq_intraday_001",
                "foreign_symbol_ownership_conflicts": 1,
                "foreign_open_order_conflicts": 0,
                "foreign_position_conflict_detected": False,
            }
        },
        "shadow_report_by_symbol": {
            "AAPL": {
                "entry_enabled": True,
                "last_gate_reason": "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed",
            }
        },
        "chain_report_by_symbol": {
            "AAPL": {
                "entry_gate": {
                    "status": "blocked",
                    "reason": "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed",
                }
            }
        },
    }

    findings = compare_shadow(qqq_obs=qqq_obs, index_diag=index_diag, symbol="AAPL")
    messages = [item.message for item in findings]
    levels = [item.level for item in findings]

    assert "Same-symbol isolation held: index_super_win blocked behind qqq-owned symbol" in messages
    assert "FAIL" not in levels


def test_compare_shadow_fails_when_index_enters_same_symbol_while_qqq_live():
    qqq_obs = {
        "ready": True,
        "strategy_state": {
            "is_traded": True,
            "side": "LONG",
            "exec_symbol": "AAPL",
        },
        "missing_required": [],
    }
    index_diag = {
        "entry_enabled_by_symbol": {"AAPL": True},
        "trader_view_by_symbol": {
            "AAPL": {
                "final_state": "entered",
                "which_gate_blocked": None,
                "broker_open_order_conflict": False,
                "broker_position_conflict": False,
                "foreign_owner_strategy_id": None,
                "last_preview": {"status": "INTENT_SUBMITTED"},
            }
        },
        "conflict_report_by_symbol": {"AAPL": {}},
        "shadow_report_by_symbol": {"AAPL": {"entry_enabled": True}},
        "chain_report_by_symbol": {"AAPL": {"runtime": {"status": "INTENT_SUBMITTED", "reason": "intent_submitted"}}},
    }

    findings = compare_shadow(qqq_obs=qqq_obs, index_diag=index_diag, symbol="AAPL")
    levels = [item.level for item in findings]
    messages = [item.message for item in findings]

    assert "FAIL" in levels
    assert "index_super_win entered while qqq already holds same exec symbol" in messages


def test_compare_shadow_fails_when_index_diag_contains_qqq_namespace_reference():
    qqq_obs = {
        "ready": True,
        "strategy_state": {"is_traded": False, "side": None, "exec_symbol": None},
        "missing_required": [],
    }
    index_diag = json.loads(
        json.dumps(
            {
                "entry_enabled_by_symbol": {"AAPL": True},
                "trader_view_by_symbol": {"AAPL": {"final_state": "no_attempt"}},
                "conflict_report_by_symbol": {"AAPL": {}},
                "shadow_report_by_symbol": {"AAPL": {"entry_enabled": True}},
                "chain_report_by_symbol": {"AAPL": {}},
                "leak": "qqqv1:qqq_intraday_001:AAPL:ENTRY",
            }
        )
    )

    findings = compare_shadow(qqq_obs=qqq_obs, index_diag=index_diag, symbol="AAPL")
    levels = [item.level for item in findings]
    assert "FAIL" in levels
