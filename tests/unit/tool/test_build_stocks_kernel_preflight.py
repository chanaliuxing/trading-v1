from __future__ import annotations

import json
from pathlib import Path

import yaml

from tool.build_stocks_kernel_preflight import build_preflight_report


def _write_config(path: Path, *, strategy_id: str = "stocks_kernel_shadow_smh_001", signal: str = "SMH", long_exec: str = "SOXL", short_exec: str = "SOXS", dry_run: bool = False, startup_preflight_enabled: bool = True) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "strategies": [
                    {
                        "id": strategy_id,
                        "enabled": False,
                        "module": "strategies.stocks_kernel_strategy",
                        "class": "StocksKernelStrategy",
                        "capital_limit_usd": 100000,
                        "params": {
                            "cadence_s": 5,
                            "ttl_ms": 2000,
                            "extras": {
                                "strategy_template": "breakout_equity",
                                "dry_run": dry_run,
                                "startup_preflight_enabled": startup_preflight_enabled,
                                "audit_root_dir": str(path.parent / "runtime" / "audit"),
                                "signal_symbol": signal,
                                "long_exec_symbol": long_exec,
                                "short_exec_symbol": short_exec,
                            },
                        },
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_preflight_passes_for_clean_smh_instance(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    broker_positions = tmp_path / "broker_positions.json"
    broker_orders = tmp_path / "broker_orders.json"
    db_positions = tmp_path / "db_positions.json"
    db_orders = tmp_path / "db_orders.json"
    _write_config(config_path)
    broker_positions.write_text(json.dumps({"SOXL": 0.0, "SOXS": 0.0}), encoding="utf-8")
    broker_orders.write_text(json.dumps([]), encoding="utf-8")
    db_positions.write_text(json.dumps([]), encoding="utf-8")
    db_orders.write_text(json.dumps([]), encoding="utf-8")

    payload = build_preflight_report(
        config_path=config_path,
        strategy_id="stocks_kernel_shadow_smh_001",
        broker_positions_path=broker_positions,
        broker_open_orders_path=broker_orders,
        db_positions_path=db_positions,
        db_orders_path=db_orders,
    )

    assert payload["ready_for_enablement"] is True
    assert not [item for item in payload["findings"] if item["level"] == "FAIL"]


def test_preflight_fails_on_protected_qqq_overlap(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    _write_config(config_path, signal="QQQ", long_exec="TQQQ", short_exec="SQQQ")

    payload = build_preflight_report(
        config_path=config_path,
        strategy_id="stocks_kernel_shadow_smh_001",
    )

    assert payload["ready_for_enablement"] is False
    messages = [item["message"] for item in payload["findings"] if item["level"] == "FAIL"]
    assert "instance overlaps protected QQQ complex" in messages


def test_preflight_fails_when_live_preflight_disabled_and_residuals_present(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    broker_positions = tmp_path / "broker_positions.json"
    db_orders = tmp_path / "db_orders.json"
    _write_config(config_path, dry_run=False, startup_preflight_enabled=False)
    broker_positions.write_text(json.dumps({"SOXL": 1.0, "SOXS": 0.0}), encoding="utf-8")
    db_orders.write_text(
        json.dumps([{"strategy_id": "stocks_kernel_shadow_smh_001", "symbol": "SOXL", "state": "NEW", "broker_order_id": None}]),
        encoding="utf-8",
    )

    payload = build_preflight_report(
        config_path=config_path,
        strategy_id="stocks_kernel_shadow_smh_001",
        broker_positions_path=broker_positions,
        db_orders_path=db_orders,
    )

    assert payload["ready_for_enablement"] is False
    messages = [item["message"] for item in payload["findings"] if item["level"] == "FAIL"]
    assert "live instance must keep startup preflight enabled" in messages
    assert "broker residual positions present" in messages
    assert "db active orders present" in messages
