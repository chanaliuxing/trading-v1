from scripts.check_spy_option_ma_readiness import _check_active_strategy, _check_extras, _check_registry


def _base_cfg():
    return {
        "strategy_registry": {
            "spy_1dte_ma10_reject_live_v2": "strategies.spy_1dte_ma10_reject_v2_strategy.SPY1DTEMA10RejectV2Strategy",
        },
        "strategies": [
            {
                "id": "spy_1dte_ma10_reject_0324_001",
                "enabled": True,
                "shadow_mode": False,
                "params": {
                    "extras": {
                        "definition_id": "SPY_MA_CROSS_STACK_CONT_V2_DUAL",
                        "kernel_mode": "pure_plugin",
                        "strict_scope_validation": True,
                        "shared_cap_group_strategy_ids": [
                            "spy_1dte_ma10_reject_0324_001",
                            "spy_1dte_ma10_reject_0324_pure_001",
                        ],
                        "live_parity_profile_id": "spy_option_ma_ext_v1",
                        "live_parity_snapshot_root": "runtime/state/decision_snapshots_live_v2",
                        "enable_live_parity_snapshot": True,
                        "enable_live_shadow_mode": False,
                    }
                },
            }
        ],
    }


def test_check_registry_accepts_spy_v2() -> None:
    errors, infos = _check_registry(_base_cfg())
    assert errors == []
    assert infos


def test_check_active_strategy_requires_enabled() -> None:
    cfg = _base_cfg()
    cfg["strategies"][0]["enabled"] = False
    errors, infos = _check_active_strategy(cfg)
    assert f"active strategy must be enabled: spy_1dte_ma10_reject_0324_001" in errors
    assert infos


def test_check_extras_requires_contract_fields() -> None:
    cfg = _base_cfg()
    extras = cfg["strategies"][0]["params"]["extras"]
    extras["definition_id"] = ""
    extras["live_parity_profile_id"] = ""
    errors, infos = _check_extras(cfg)
    assert "definition_id mismatch: expected SPY_MA_CROSS_STACK_CONT_V2_DUAL" in errors
    assert "live_parity_profile_id missing" in errors
    assert infos
