from __future__ import annotations

from strategies.index_super_win_replica_gate import evaluate_replica_entry_gate


def test_gate_blocks_market_data_unavailable() -> None:
    decision = evaluate_replica_entry_gate(
        features={"market_data_healthy": False},
        extras={"entry_require_market_data_healthy": True},
    )

    assert decision.allowed is False
    assert decision.reason == "market_data_unavailable"


def test_gate_blocks_outside_ab_time_window() -> None:
    decision = evaluate_replica_entry_gate(
        features={"now_time_et": "09:29:59"},
        extras={
            "stable_entry_guard_enabled": True,
            "ab_time_start": "09:30:00",
            "ab_time_end": "15:59:00",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "outside_ab_window"
    assert decision.details["ab_time_start"] == "09:30:00"
    assert decision.details["ab_time_end"] == "15:59:00"


def test_gate_blocks_after_no_new_entry_time() -> None:
    decision = evaluate_replica_entry_gate(
        features={"now_time_et": "14:50:00"},
        extras={
            "stable_entry_guard_enabled": True,
            "ab_time_start": "09:30:00",
            "ab_time_end": "15:59:00",
            "no_new_entry_after": "14:50:00",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "after_no_new_entry"
    assert decision.details["no_new_entry_after"] == "14:50:00"


def test_gate_blocks_required_data_missing() -> None:
    decision = evaluate_replica_entry_gate(
        features={"required_data_missing": ["QQQ", "QQEW"]},
        extras={"stable_entry_guard_enabled": True},
    )

    assert decision.allowed is False
    assert decision.reason == "required_data_missing"
    assert decision.details["missing_required"] == ["QQQ", "QQEW"]


def test_gate_blocks_data_integrity_when_qqq_stale() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "qqq_age_sec": 4.2,
            "qqew_age_sec": 1.0,
            "now_time_et": "10:01:00",
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "fail_closed_on_data_integrity": True,
            "entry_max_data_age_sec": 3.0,
        },
    )

    assert decision.allowed is False
    assert decision.reason == "data_integrity_fail_closed"
    assert decision.details["integrity_failure"]["symbol"] == "QQQ"


def test_gate_reports_missing_primary_freshness_field_separately() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "qqew_age_sec": 1.0,
            "now_time_et": "18:24:00",
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "fail_closed_on_data_integrity": True,
            "entry_max_data_age_sec": 5.0,
        },
    )

    assert decision.allowed is False
    assert decision.reason == "data_integrity_fail_closed"
    assert decision.details["integrity_failure"]["kind"] == "freshness_field_missing"
    assert decision.details["integrity_failure"]["symbol"] == "QQQ"
    assert decision.details["integrity_failure"]["field"] == "qqq_age_sec"


def test_gate_confirm_symbol_defaults_to_primary_symbol_when_not_configured() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "qqq_age_sec": 1.0,
            "now_time_et": "18:24:00",
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "fail_closed_on_data_integrity": True,
            "entry_max_data_age_sec": 5.0,
            "signal_symbol": "QQQ",
        },
    )

    assert decision.allowed is True
    assert decision.reason is None


def test_gate_blocks_data_integrity_when_opening_price_base_source_unknown() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "qqq_age_sec": 1.0,
            "qqew_age_sec": 1.0,
            "now_time_et": "09:45:00",
            "price_base_source": "unknown",
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "fail_closed_on_data_integrity": True,
        },
    )

    assert decision.allowed is False
    assert decision.reason == "data_integrity_fail_closed"
    assert decision.details["integrity_failure"]["kind"] == "price_base_source_unknown"


def test_gate_normalizes_missing_required_dict_items() -> None:
    decision = evaluate_replica_entry_gate(
        features={"missing_required": [{"symbol": "qqq"}, {"field": "qqew"}, {"name": "atr_live"}]},
        extras={"stable_entry_guard_enabled": True},
    )

    assert decision.allowed is False
    assert decision.reason == "required_data_missing"
    assert decision.details["missing_required"] == ["QQQ", "QQEW", "ATR_LIVE"]


def test_gate_blocks_restart_degrade_active() -> None:
    decision = evaluate_replica_entry_gate(
        features={"restart_degrade_active": True},
        extras={"stable_entry_guard_enabled": True},
    )

    assert decision.allowed is False
    assert decision.reason == "restart_degrade_active"


def test_gate_blocks_startup_anchor_embargo_from_source_and_time_window() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "now_time_et": "09:40:00",
            "price_base_source": "startup_anchor_10s",
        },
        extras={
            "stable_entry_guard_enabled": True,
            "startup_anchor_entry_block_enabled": True,
            "startup_anchor_entry_block_start": "09:30:00",
            "startup_anchor_entry_block_end": "10:00:00",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "startup_anchor_embargo"
    assert decision.details["price_base_source"] == "startup_anchor_10s"


def test_gate_blocks_first_entry_seed_not_ready() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "first_entry_guard_context": True,
            "seed_ready": False,
            "seed_sample_count": 8,
        },
        extras={
            "stable_entry_guard_enabled": True,
            "first_entry_require_seed_ready": True,
            "entry_seed_min_samples": 12,
        },
    )

    assert decision.allowed is False
    assert decision.reason == "first_entry_seed_not_ready"
    assert decision.details["seed_sample_count"] == 8


def test_gate_blocks_stale_qqq_freshness() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "qqq_age_sec": 9.0,
            "qqew_age_sec": 1.0,
            "data_ready": True,
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "freshness_max_sec": 5.0,
        },
    )

    assert decision.allowed is False
    assert decision.reason == "data_not_ready"
    assert decision.details["freshness_breach"]["symbol"] == "QQQ"


def test_gate_blocks_direction_mismatch_against_vwap_for_long() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "intent_side": "LONG",
            "qqq_close_now": 499.0,
            "vwap_now": 500.0,
            "data_ready": True,
        },
        extras={"stable_entry_guard_enabled": True},
    )

    assert decision.allowed is False
    assert decision.reason == "direction_mismatch"
    assert decision.details["direction"] == "LONG"


def test_gate_blocks_qqew_confirm_direction_mismatch() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "intent_side": "SHORT",
            "qqq_close_now": 499.0,
            "vwap_now": 500.0,
            "qqew_close_now": 500.5,
            "qqew_vwap_now": 500.0,
            "data_ready": True,
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "require_confirm_vwap_ready": True,
            "confirm_symbol": "QQEW",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "direction_mismatch"
    assert decision.details["direction"] == "SHORT"
    assert decision.details["qqew_close_now"] == 500.5


def test_gate_allows_when_system_and_data_are_ready() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "decision_pipeline_healthy": True,
            "market_data_healthy": True,
            "data_ready": True,
            "required_data_missing": [],
        },
        extras={"stable_entry_guard_enabled": True},
    )

    assert decision.allowed is True
    assert decision.reason is None


def test_gate_blocks_missing_continuity_when_required() -> None:
    decision = evaluate_replica_entry_gate(
        features={"data_ready": True, "continuous_180s": False},
        extras={"stable_entry_guard_enabled": True, "require_continuity_180s": True},
    )

    assert decision.allowed is False
    assert decision.reason == "data_not_ready"
    assert decision.details["continuity"] == "missing_180s"


def test_gate_blocks_missing_vwap_for_directional_entry() -> None:
    decision = evaluate_replica_entry_gate(
        features={"intent_side": "LONG", "qqq_close_now": 500.0, "vwap_now": 0.0, "data_ready": True},
        extras={"stable_entry_guard_enabled": True, "require_vwap_ready": True},
    )

    assert decision.allowed is False
    assert decision.reason == "data_not_ready"
    assert decision.details["missing"] == "QQQ:VWAP"


def test_gate_blocks_missing_confirm_vwap_when_confirm_symbol_required() -> None:
    decision = evaluate_replica_entry_gate(
        features={"intent_side": "LONG", "qqq_close_now": 501.0, "vwap_now": 500.0, "qqew_close_now": 10.0, "qqew_vwap_now": 0.0, "data_ready": True},
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "require_confirm_vwap_ready": True,
            "confirm_symbol": "QQEW",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "data_not_ready"
    assert decision.details["missing"] == "QQEW:VWAP"


def test_gate_skips_confirm_requirements_when_qqew_decoupled() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "intent_side": "LONG",
            "qqq_close_now": 501.0,
            "vwap_now": 500.0,
            "qqew_close_now": 10.0,
            "qqew_vwap_now": 0.0,
            "qqew_decoupled": True,
            "data_ready": True,
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "require_confirm_vwap_ready": True,
            "confirm_symbol": "QQEW",
        },
    )

    assert decision.allowed is True
    assert decision.reason is None


def test_gate_supports_binding_driven_primary_and_confirm_symbols() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "intent_side": "LONG",
            "spy_close_now": 499.0,
            "spy_vwap_now": 500.0,
            "ivw_close_now": 501.0,
            "ivw_vwap_now": 500.0,
            "data_ready": True,
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "gate_primary_symbol": "SPY",
            "gate_primary_feature_prefix": "spy",
            "gate_confirm_symbol": "IVW",
            "gate_confirm_feature_prefix": "ivw",
            "require_confirm_vwap_ready": True,
        },
    )

    assert decision.allowed is False
    assert decision.reason == "direction_mismatch"
    assert decision.details["spy_close_now"] == 499.0


def test_gate_reports_dynamic_missing_confirm_vwap_symbol_name() -> None:
    decision = evaluate_replica_entry_gate(
        features={
            "intent_side": "LONG",
            "spy_close_now": 501.0,
            "spy_vwap_now": 500.0,
            "ivw_close_now": 10.0,
            "ivw_vwap_now": 0.0,
            "data_ready": True,
        },
        extras={
            "stable_entry_guard_enabled": True,
            "use_confirm_symbol": True,
            "require_confirm_vwap_ready": True,
            "gate_primary_symbol": "SPY",
            "gate_primary_feature_prefix": "spy",
            "gate_confirm_symbol": "IVW",
            "gate_confirm_feature_prefix": "ivw",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "data_not_ready"
    assert decision.details["missing"] == "IVW:VWAP"


def test_gate_blocks_missing_atr_when_required() -> None:
    decision = evaluate_replica_entry_gate(
        features={"data_ready": True, "atr_live": 0.0, "atr_seed": None, "atr_cap": 0.0},
        extras={"stable_entry_guard_enabled": True, "require_atr_ready": True},
    )

    assert decision.allowed is False
    assert decision.reason == "data_not_ready"
    assert decision.details["missing"] == "ATR"


def test_gate_blocks_shadow_extreme_distance_from_vwap_atr() -> None:
    decision = evaluate_replica_entry_gate(
        features={"data_ready": True, "distance_from_vwap_atr": 9.4},
        extras={
            "stable_entry_guard_enabled": True,
            "entry_shadow_extreme_guard_enabled": True,
            "entry_shadow_extreme_distance_from_vwap_atr_min": 8.1,
        },
    )

    assert decision.allowed is False
    assert decision.reason == "shadow_extreme_blocked"
    assert decision.details["distance_from_vwap_atr"] == 9.4
