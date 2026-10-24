from scripts.check_qqq_option_v2_readiness import (
    _check_parity_extras,
    _check_registry,
    _check_shared_cap_group,
    _check_strategy_presence,
)


def _base_cfg():
    return {
        "strategy_registry": {
            "qqq_1dte_ma10_reject_live_v2": "strategies.qqq_1dte_ma10_reject_v2_strategy.QQQ1DTEMA10RejectV2Strategy",
        },
        "strategies": [
            {
                "id": "qqq_1dte_ma10_reject_0324_001",
                "enabled": True,
                "params": {"extras": {}},
            },
            {
                "id": "qqq_1dte_ma10_reject_0324_v2_001",
                "enabled": False,
                "params": {
                    "extras": {
                        "shared_cap_group_id": "QQQ_OPT_LIVE_SHARED",
                        "shared_cap_group_strategy_ids": [
                            "qqq_0dte_opt_live_001",
                            "qqq_1dte_ma10_reject_0324_001",
                            "qqq_1dte_ma10_reject_0324_v2_001",
                        ],
                        "live_parity_profile_id": "qqq_option_ma_ext_v1",
                        "live_parity_snapshot_root": "runtime/state/decision_snapshots",
                        "enable_live_parity_snapshot": True,
                    }
                },
            },
        ],
    }


def test_check_registry_accepts_v2():
    errors, infos = _check_registry(_base_cfg())
    assert errors == []
    assert infos


def test_check_strategy_presence_requires_v2_disabled():
    cfg = _base_cfg()
    cfg["strategies"][1]["enabled"] = True
    errors, infos = _check_strategy_presence(cfg)
    assert "v2 must remain disabled by default: qqq_1dte_ma10_reject_0324_v2_001" in errors
    assert infos


def test_check_strategy_presence_accepts_shadow_mode_enablement():
    cfg = _base_cfg()
    cfg["strategies"][1]["enabled"] = True
    cfg["strategies"][1]["shadow_mode"] = True
    errors, infos = _check_strategy_presence(cfg, mode="shadow")
    assert errors == []
    assert "v2 enabled for shadow mode: qqq_1dte_ma10_reject_0324_v2_001" in infos
    assert "v2 shadow_mode enabled: qqq_1dte_ma10_reject_0324_v2_001" in infos


def test_check_strategy_presence_accepts_live_mode_cutover():
    cfg = _base_cfg()
    cfg["strategies"][0]["enabled"] = False
    cfg["strategies"][1]["enabled"] = True
    cfg["strategies"][1]["shadow_mode"] = False
    errors, infos = _check_strategy_presence(cfg, mode="live")
    assert errors == []
    assert "legacy disabled for v2 live mode: qqq_1dte_ma10_reject_0324_001" in infos
    assert "v2 enabled for live mode: qqq_1dte_ma10_reject_0324_v2_001" in infos
    assert "v2 shadow_mode disabled for live mode: qqq_1dte_ma10_reject_0324_v2_001" in infos


def test_check_shared_cap_group_requires_all_members():
    cfg = _base_cfg()
    cfg["strategies"][1]["params"]["extras"]["shared_cap_group_strategy_ids"] = [
        "qqq_0dte_opt_live_001",
        "qqq_1dte_ma10_reject_0324_001",
    ]
    errors, infos = _check_shared_cap_group(cfg)
    assert errors == ["v2 shared_cap_group_strategy_ids missing: qqq_1dte_ma10_reject_0324_v2_001"]
    assert infos


def test_check_parity_extras_requires_profile_snapshot_and_enable():
    cfg = _base_cfg()
    extras = cfg["strategies"][1]["params"]["extras"]
    extras["live_parity_profile_id"] = ""
    extras["live_parity_snapshot_root"] = ""
    extras["enable_live_parity_snapshot"] = False
    errors, infos = _check_parity_extras(cfg)
    assert "v2 live_parity_profile_id missing" in errors
    assert "v2 live_parity_snapshot_root missing" in errors
    assert "v2 enable_live_parity_snapshot must be true" in errors
    assert infos == []


def test_check_parity_extras_requires_live_shadow_flag_in_shadow_mode():
    cfg = _base_cfg()
    errors, infos = _check_parity_extras(cfg, mode="shadow")
    assert "v2 enable_live_shadow_mode must be true in shadow mode" in errors
    assert "v2 live_parity_profile_id ok: qqq_option_ma_ext_v1" in infos


def test_check_parity_extras_requires_shadow_flag_off_in_live_mode():
    cfg = _base_cfg()
    cfg["strategies"][1]["params"]["extras"]["enable_live_shadow_mode"] = True
    errors, infos = _check_parity_extras(cfg, mode="live")
    assert "v2 enable_live_shadow_mode must be false in live mode" in errors
    assert "v2 live_parity_profile_id ok: qqq_option_ma_ext_v1" in infos
