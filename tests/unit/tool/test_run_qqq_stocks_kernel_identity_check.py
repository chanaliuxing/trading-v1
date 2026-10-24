import json
from pathlib import Path

import pytest

from tool.replay.run_qqq_stocks_kernel_identity_check import _load_strategy_extras, _run


@pytest.mark.asyncio
async def test_identity_check_runs_and_returns_parity_payload(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
strategies:
  - id: qqq_intraday_001
    params:
      extras:
        entry_model: v47_fast_drift
        qqew_decoupled: true
        ab_time_start: "09:30:00"
        ab_time_end: "14:00:00"
        delayed_confirm_end: "10:00:00"
        delayed_confirm_move5m_min: 0.18
        enable_fast: true
        drift_bar_min_atr: 0.1
        drift_cum_atr_min: 0.45
        fast_v3m_atr_min: 0.6
        fast_vol_min: 2.0
        atr_rank_skip_max: 0.15
        early_override_window_start: "09:39:00"
        early_override_window_end: "09:45:00"
        early_override_move_mult: 0.14
        early_override_vol_min: 0.65
""".strip()
        + "\n",
        encoding="utf-8",
    )
    diag_path = tmp_path / "diag.jsonl"
    rows = [
        {
            "event": "entry_gate",
            "ts_now_et": "2026-04-08T10:00:00-04:00",
            "decision": "ENTRY_ALLOWED",
            "reason": "entry_allowed",
            "direction": "LONG",
            "instrument": "TQQQ",
            "qqq_close_now": 500.0,
            "vwap_now": 499.0,
            "atr_eff": 1.0,
            "atr_seed": 1.0,
            "atr_live": 1.0,
            "v3m_signed": 1.2,
            "vol_t": 1000.0,
            "data_age_qqq_sec": 0.2,
            "data_age_qqew_sec": 0.2,
            "stream_invalid": False,
            "seed_n": 800,
        },
        {
            "event": "entry_gate",
            "ts_now_et": "2026-04-08T10:01:00-04:00",
            "decision": "BLOCKED",
            "reason": "sign_mismatch",
            "direction": "NA",
            "instrument": "NA",
            "qqq_close_now": 500.2,
            "vwap_now": 499.9,
            "atr_eff": 1.0,
            "atr_seed": 1.0,
            "atr_live": 1.0,
            "v3m_signed": -0.1,
            "vol_t": 900.0,
            "data_age_qqq_sec": 0.2,
            "data_age_qqew_sec": 0.2,
            "stream_invalid": False,
            "seed_n": 800,
        },
    ]
    diag_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows), encoding="utf-8")

    payload = await _run(
        diag_path=diag_path,
        config_path=config_path,
        strategy_id="qqq_intraday_001",
        day="2026-04-08",
        max_rows=0,
    )

    assert payload["ok"] is True
    assert payload["rows"] == 2
    assert "parity" in payload
    assert "allow_match_ratio" in payload["parity"]


def test_load_strategy_extras_merges_nested_policy_params(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
strategies:
  - id: qqq_intraday_plugin_001
    params:
      extras:
        policy_params:
          entry_model: v47_fast_drift
          qqew_decoupled: true
          first_entry_seed_min_samples: 600
""".strip()
        + "\n",
        encoding="utf-8",
    )

    extras = _load_strategy_extras(config_path, strategy_id="qqq_intraday_plugin_001")

    assert extras["entry_model"] == "v47_fast_drift"
    assert extras["qqew_decoupled"] is True
    assert extras["first_entry_seed_min_samples"] == 600
