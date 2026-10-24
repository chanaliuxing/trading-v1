from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from strategies.stocks_kernel.config_loader import load_kernel_config
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy


ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config.yml"
SHADOW_IDS = ("qqq_kernel_shadow_legacy_on_001", "qqq_kernel_shadow_legacy_off_001")


def _strategy_instances() -> dict[str, dict]:
    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return {str(item.get("id") or ""): item for item in list(payload.get("strategies") or [])}


def test_qqq_kernel_shadow_instances_are_present_and_enabled_for_dry_run_collection():
    instances = _strategy_instances()

    for strategy_id in SHADOW_IDS:
        item = instances[strategy_id]
        assert item["enabled"] is True
        assert item["registry_name"] == "stocks_kernel"
        assert item["module"] == "strategies.stocks_kernel_strategy"
        assert item["class"] == "StocksKernelStrategy"


def test_qqq_kernel_shadow_instances_are_dry_run_and_shadow_only():
    instances = _strategy_instances()

    for strategy_id in SHADOW_IDS:
        extras = instances[strategy_id]["params"]["extras"]
        assert extras["dry_run"] is True
        assert extras["shadow_only"] is True
        assert extras["shadow_origin_strategy_id"] == "qqq_intraday_001"
        assert extras["enable_shared_underlying_data_plane"] is True
        assert extras["shared_underlying_read_mode"] == "plane_only"
        assert extras["qqq_shadow_trace_enabled"] is True
        assert extras["qqq_shadow_trace_root"] == "reports/qqq_signal_snapshots"
        assert extras["policy_id"] == "qqq_intraday_policy_v1"
        assert extras["strategy_template"] == "qqq_intraday_core"
        assert extras["signal_symbol"] == "QQQ"
        assert extras["long_exec_symbol"] == "TQQQ"
        assert extras["short_exec_symbol"] == "SQQQ"


def test_qqq_kernel_shadow_legacy_modes_are_explicit_and_distinct():
    instances = _strategy_instances()

    on_extras = instances["qqq_kernel_shadow_legacy_on_001"]["params"]["extras"]
    off_extras = instances["qqq_kernel_shadow_legacy_off_001"]["params"]["extras"]
    assert on_extras["shadow_legacy_mode"] == "legacy_on"
    assert on_extras["policy_params"]["legacy_trader_parity_mode"] is True
    assert off_extras["shadow_legacy_mode"] == "legacy_off"
    assert off_extras["policy_params"]["legacy_trader_parity_mode"] is False


def test_qqq_kernel_shadow_config_loader_resolves_policy_and_instruments():
    instances = _strategy_instances()

    for strategy_id in SHADOW_IDS:
        params = SimpleNamespace(extras=instances[strategy_id]["params"]["extras"])
        config = load_kernel_config(strategy_id, params)
        assert config.policy_id == "qqq_intraday_policy_v1"
        assert config.dry_run is True
        assert config.raw_extras["shadow_only"] is True
        assert config.raw_extras["enable_shared_underlying_data_plane"] is True
        assert config.raw_extras["shared_underlying_read_mode"] == "plane_only"
        assert len(config.instruments) == 1
        instrument = config.instruments[0]
        assert instrument.signal_symbol == "QQQ"
        assert instrument.long_exec_symbol == "TQQQ"
        assert instrument.short_exec_symbol == "SQQQ"


def test_shadow_orders_origin_field_migration_exists():
    migration = ROOT / "db" / "pg" / "migration_20260420_shadow_orders_origin_fields.ddl"
    text = migration.read_text(encoding="utf-8")

    assert "origin_strategy_id" in text
    assert "legacy_mode" in text
    assert "shadow_profile_id" in text


def test_shadow_only_requires_dry_run_true():
    params = SimpleNamespace(
        extras={
            "shadow_only": True,
            "dry_run": False,
            "policy_id": "qqq_intraday_policy_v1",
            "signal_symbol": "QQQ",
            "long_exec_symbol": "TQQQ",
            "short_exec_symbol": "SQQQ",
        }
    )

    try:
        load_kernel_config("qqq_kernel_shadow_bad_001", params)
    except ValueError as exc:
        assert "shadow_only=true requires dry_run=true" in str(exc)
    else:
        raise AssertionError("expected ValueError for shadow_only without dry_run")


def test_qqq_kernel_shadow_trace_writer_emits_jsonl(tmp_path: Path):
    strategy = ComposableStocksKernelStrategy.__new__(ComposableStocksKernelStrategy)
    strategy.strategy_id = "qqq_kernel_shadow_legacy_on_001"
    strategy._qqq_shadow_trace_enabled = True
    strategy._qqq_shadow_trace_root = tmp_path
    strategy.config = SimpleNamespace(dry_run=True, raw_extras={"shadow_legacy_mode": "legacy_on"})
    strategy._audit = lambda **kwargs: None

    strategy._persist_qqq_shadow_trace(
        snapshot=SimpleNamespace(symbol="QQQ", ts_epoch_s=1770000000.0),
        signal=SimpleNamespace(action="enter"),
        entry_plan=SimpleNamespace(symbol="TQQQ", side="LONG", qty=10, trigger_id="trigger-1"),
        action_index=1,
        action_total=1,
    )

    paths = list(tmp_path.glob("qqq_signal_snapshots_*_qqq_kernel_shadow_legacy_on_001.jsonl"))
    assert len(paths) == 1
    row = json.loads(paths[0].read_text(encoding="utf-8").strip())
    assert row["strategy_id"] == "qqq_kernel_shadow_legacy_on_001"
    assert row["snapshot_kind"] == "entry"
    assert row["signal_authority"]["side"] == "LONG"
    assert row["intent"]["symbol"] == "TQQQ"
    assert row["order_plan"]["order_type"] == "DRY_RUN_SHADOW"
