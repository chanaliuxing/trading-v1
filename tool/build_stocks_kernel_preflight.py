#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks_kernel.config_loader import load_kernel_config

PROTECTED_QQQ_COMPLEX = {"QQQ", "TQQQ", "SQQQ"}


@dataclass(frozen=True)
class Finding:
    level: str
    message: str
    details: dict[str, Any]


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _is_writable_path(path: Path) -> bool:
    target_dir = path if path.is_dir() else path.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        probe = target_dir / ".stocks_kernel_preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _normalize_broker_positions(payload: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(payload, dict):
        for symbol, qty in payload.items():
            key = str(symbol or "").strip().upper()
            if not key:
                continue
            out[key] = float(qty or 0.0)
        return out
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            key = str(item.get("symbol") or "").strip().upper()
            if not key:
                continue
            out[key] = float(item.get("qty") or 0.0)
    return out


def _normalize_broker_open_orders(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return rows
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "order_ref": str(item.get("order_ref") or "").strip(),
                "broker_order_id": str(item.get("broker_order_id") or "").strip() or None,
                "perm_id": item.get("perm_id"),
            }
        )
    return rows


def _normalize_db_positions(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "strategy_id": str(item.get("strategy_id") or "").strip(),
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "pos_qty": float(item.get("pos_qty") or 0.0),
            }
        )
    return out


def _normalize_db_orders(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "strategy_id": str(item.get("strategy_id") or "").strip(),
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "state": str(item.get("state") or "").strip().upper(),
                "broker_order_id": str(item.get("broker_order_id") or "").strip() or None,
            }
        )
    return out


def build_preflight_report(
    *,
    config_path: Path,
    strategy_id: str,
    broker_positions_path: Path | None = None,
    broker_open_orders_path: Path | None = None,
    db_positions_path: Path | None = None,
    db_orders_path: Path | None = None,
) -> dict[str, Any]:
    findings: list[Finding] = []
    cfg = _load_yaml(config_path)
    strategies = list(cfg.get("strategies") or [])
    strategy_block = next((item for item in strategies if str(item.get("id") or "").strip() == strategy_id), None)
    if strategy_block is None:
        return {
            "ready_for_enablement": False,
            "strategy_id": strategy_id,
            "findings": [{"level": "FAIL", "message": "strategy block not found", "details": {"config_path": str(config_path)}}],
        }

    module = str(strategy_block.get("module") or "").strip()
    klass = str(strategy_block.get("class") or "").strip()
    enabled = bool(strategy_block.get("enabled"))
    if module == "strategies.stocks_kernel_strategy" and klass == "StocksKernelStrategy":
        findings.append(Finding("PASS", "strategy module/class valid", {"module": module, "class": klass}))
    else:
        findings.append(Finding("FAIL", "strategy module/class invalid", {"module": module, "class": klass}))

    params_payload = dict((strategy_block.get("params") or {}))
    extras = dict(params_payload.get("extras") or {})
    cadence_s = int(params_payload.get("cadence_s") or 5)
    ttl_ms = int(params_payload.get("ttl_ms") or 2000)
    config = load_kernel_config(strategy_id, type("Params", (), {"extras": extras})())

    if config.instruments:
        findings.append(Finding("PASS", "instrument bindings loaded", {"count": len(config.instruments)}))
    else:
        findings.append(Finding("FAIL", "instrument bindings missing", {}))

    if cadence_s > 0 and ttl_ms > 0:
        findings.append(Finding("PASS", "cadence/ttl valid", {"cadence_s": cadence_s, "ttl_ms": ttl_ms}))
    else:
        findings.append(Finding("FAIL", "cadence/ttl invalid", {"cadence_s": cadence_s, "ttl_ms": ttl_ms}))

    if not config.dry_run and not config.startup_preflight_enabled:
        findings.append(Finding("FAIL", "live instance must keep startup preflight enabled", {}))
    else:
        findings.append(
            Finding(
                "PASS",
                "startup preflight policy valid",
                {"dry_run": config.dry_run, "startup_preflight_enabled": config.startup_preflight_enabled},
            )
        )

    protected_overlap = sorted(
        {
            symbol
            for instrument in config.instruments
            for symbol in {instrument.signal_symbol, instrument.long_exec_symbol, instrument.short_exec_symbol}
            if symbol in PROTECTED_QQQ_COMPLEX
        }
    )
    if protected_overlap:
        findings.append(Finding("FAIL", "instance overlaps protected QQQ complex", {"symbols": protected_overlap}))
    else:
        findings.append(Finding("PASS", "instance isolated from protected QQQ complex", {}))

    exec_symbols = sorted(
        {
            instrument.long_exec_symbol
            for instrument in config.instruments
        }
        | {
            instrument.short_exec_symbol
            for instrument in config.instruments
        }
    )

    if _is_writable_path(Path(config.audit_root_dir)):
        findings.append(Finding("PASS", "audit root writable", {"audit_root_dir": config.audit_root_dir}))
    else:
        findings.append(Finding("FAIL", "audit root not writable", {"audit_root_dir": config.audit_root_dir}))

    broker_positions = _normalize_broker_positions(_load_json(broker_positions_path))
    if broker_positions_path is not None:
        residuals = {symbol: qty for symbol, qty in broker_positions.items() if symbol in exec_symbols and abs(float(qty)) > 1e-12}
        if residuals:
            findings.append(Finding("FAIL", "broker residual positions present", {"positions": residuals}))
        else:
            findings.append(Finding("PASS", "broker positions flat for exec symbols", {"symbols": exec_symbols}))

    broker_open_orders = _normalize_broker_open_orders(_load_json(broker_open_orders_path))
    if broker_open_orders_path is not None:
        residual_orders = [item for item in broker_open_orders if item["symbol"] in exec_symbols]
        if residual_orders:
            findings.append(Finding("FAIL", "broker open orders present", {"orders": residual_orders}))
        else:
            findings.append(Finding("PASS", "broker open orders clean", {"symbols": exec_symbols}))

    db_positions = _normalize_db_positions(_load_json(db_positions_path))
    if db_positions_path is not None:
        residual_positions = [
            item
            for item in db_positions
            if (item["symbol"] in exec_symbols or item["strategy_id"] == strategy_id) and abs(float(item["pos_qty"])) > 1e-12
        ]
        if residual_positions:
            findings.append(Finding("FAIL", "db residual positions present", {"positions": residual_positions}))
        else:
            findings.append(Finding("PASS", "db positions clean", {"symbols": exec_symbols, "strategy_id": strategy_id}))

    db_orders = _normalize_db_orders(_load_json(db_orders_path))
    if db_orders_path is not None:
        residual_orders = [
            item
            for item in db_orders
            if (item["symbol"] in exec_symbols or item["strategy_id"] == strategy_id)
            and item["state"] not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}
        ]
        if residual_orders:
            findings.append(Finding("FAIL", "db active orders present", {"orders": residual_orders}))
        else:
            findings.append(Finding("PASS", "db active orders clean", {"symbols": exec_symbols, "strategy_id": strategy_id}))

    ready = not any(item.level == "FAIL" for item in findings)
    return {
        "ready_for_enablement": ready,
        "strategy_id": strategy_id,
        "enabled": enabled,
        "config_path": str(config_path),
        "exec_symbols": exec_symbols,
        "findings": [{"level": item.level, "message": item.message, "details": item.details} for item in findings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build preflight readiness report for a stocks_kernel strategy instance.")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--broker-positions")
    parser.add_argument("--broker-open-orders")
    parser.add_argument("--db-positions")
    parser.add_argument("--db-orders")
    parser.add_argument("--output-path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build_preflight_report(
        config_path=Path(args.config),
        strategy_id=str(args.strategy_id),
        broker_positions_path=Path(args.broker_positions) if args.broker_positions else None,
        broker_open_orders_path=Path(args.broker_open_orders) if args.broker_open_orders else None,
        db_positions_path=Path(args.db_positions) if args.db_positions else None,
        db_orders_path=Path(args.db_orders) if args.db_orders else None,
    )
    if args.output_path:
        output = Path(args.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"ready_for_enablement={payload['ready_for_enablement']}")
        for item in payload["findings"]:
            suffix = f" {json.dumps(item['details'], ensure_ascii=False, sort_keys=True)}" if item["details"] else ""
            print(f"[{item['level']}] {item['message']}{suffix}")
    return 0 if payload["ready_for_enablement"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
