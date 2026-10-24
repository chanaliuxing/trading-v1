from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import json

from scripts.check_qqq_option_dual_live_readiness import (
    _check_runtime_state,
    _check_shared_cap_group,
    _check_telegram_allowlist,
)


ET = ZoneInfo("America/New_York")


def test_check_shared_cap_group_accepts_matching_settings():
    cfg = {
        "strategies": [
            {
                "id": "qqq_0dte_opt_live_001",
                "params": {"extras": {
                    "shared_cap_group_id": "QQQ_OPT_LIVE_SHARED",
                    "shared_cap_group_limit_usd": 20000,
                    "shared_cap_group_strategy_ids": [
                        "qqq_1dte_ma10_reject_0324_001",
                        "qqq_0dte_opt_live_001",
                    ],
                }},
            },
            {
                "id": "qqq_1dte_ma10_reject_0324_001",
                "params": {"extras": {
                    "shared_cap_group_id": "QQQ_OPT_LIVE_SHARED",
                    "shared_cap_group_limit_usd": 20000,
                    "shared_cap_group_strategy_ids": [
                        "qqq_0dte_opt_live_001",
                        "qqq_1dte_ma10_reject_0324_001",
                    ],
                }},
            },
        ]
    }
    errors, infos = _check_shared_cap_group(cfg)
    assert errors == []
    assert len(infos) == 2


def test_check_telegram_allowlist_requires_both_strategies():
    cfg = {"notifications": {"telegram": {"strategy_allowlist": ["qqq_0dte_opt_live_001"]}}}
    errors, infos = _check_telegram_allowlist(cfg)
    assert infos
    assert errors == ["telegram allowlist missing: qqq_1dte_ma10_reject_0324_001"]


def test_check_runtime_state_flags_stale_file(tmp_path: Path, monkeypatch):
    state_dir = tmp_path / "runtime" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "qqq_1dte_ma10_reject_live_qqq_1dte_ma10_reject_0324_001.json"
    state_path.write_text(json.dumps({"day": "2026-03-24", "reason": "test"}), encoding="utf-8")
    monkeypatch.setattr("scripts.check_qqq_option_dual_live_readiness.ROOT", tmp_path)
    errors, infos = _check_runtime_state("qqq_1dte_ma10_reject_0324_001", datetime(2026, 3, 25, 9, 0, tzinfo=ET))
    assert infos
    assert len(errors) == 1
    assert "stale runtime state" in errors[0]
