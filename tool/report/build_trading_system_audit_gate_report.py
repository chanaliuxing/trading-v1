#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

VERDICTS = {"PASS", "FAIL", "INSUFFICIENT_DATA"}
MODES = {"quick", "full", "pre_canary"}
DIAGNOSTIC_TTL_SEC = 24 * 60 * 60
PRE_CANARY_MIN_SAMPLE_COUNT = 20

REQUIRED_COMMANDS_BY_MODE: dict[str, tuple[str, ...]] = {
    "quick": (
        "no_live_guard",
        "git_diff_check",
        "exec_contract",
        "services_contract",
        "ib_reconnect_fast",
    ),
    "full": (
        "no_live_guard",
        "git_diff_check",
        "exec_contract",
        "services_contract",
        "ib_reconnect_fast",
        "reconciliation_contract",
        "reports_contract",
        "ib_reconnect_full",
    ),
    "pre_canary": (
        "no_live_guard",
        "git_diff_check",
        "exec_contract",
        "services_contract",
        "ib_reconnect_fast",
        "reconciliation_contract",
        "reports_contract",
        "ib_reconnect_full",
        "bounded_shadow_runtime",
        "bounded_paper_runtime",
        "strategy_execution_reliability_report",
        "approved_to_fill_conversion_report",
        "vegas_dlb_edge_measurement_report",
        "paper_shadow_soak_gate_report",
    ),
}


@dataclass(frozen=True)
class ScanRule:
    risk: str
    pattern: re.Pattern[str]
    severity: str = "critical"


SCAN_RULES: tuple[ScanRule, ...] = (
    ScanRule("direct_ib_constructor", re.compile(r"\bIB\s*\(")),
    ScanRule("direct_ib_connect_async", re.compile(r"\bconnectAsync\s*\(")),
    ScanRule("broker_order_mutation", re.compile(r"\b(?:placeOrder|cancelOrder|modifyOrder)\s*\(")),
    ScanRule("order_ref_prefix_owner_heuristic", re.compile(r"order_ref.*startswith\s*\(|startswith\s*\(.*order_ref|order_ref_(?:prefix|substring)", re.I)),
    ScanRule("symbol_only_attribution", re.compile(r"\bsymbol[_-]?only\b|attribution_path.*symbol", re.I)),
    ScanRule("symbol_only_flatten", re.compile(r"flatten\s*\(\s*symbol|flatten.*symbol_only", re.I)),
    ScanRule("symbol_only_cancel", re.compile(r"cancel.*symbol_only|symbol_only.*cancel|cancel.*by_symbol", re.I)),
    ScanRule("auto_reconciler_mutation", re.compile(r"\bauto_(?:cancel|flatten|terminalize|adopt)\b", re.I)),
    ScanRule("broker_aggregate_overwrite", re.compile(r"broker_aggregate.*(?:overwrite|overwrote)|strategy.*position.*broker_aggregate", re.I)),
    ScanRule("db_projection_used_as_truth", re.compile(r"db_projection_only|broker_truth_backed\s*[:=]\s*False", re.I)),
)

DEFAULT_SCAN_DIRS = ("services", "strategies", "tool", "scripts")
DEFAULT_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "direct_ib_constructor": (
        "services/exec/adapters/ib_adapter.py",
        "services/ibkr/ib_connection_manager.py",
        "services/market/ib_market_data.py",
        "tool/ops/broker_truth_convergence.py",
        "tool/cleanup_strategy_residuals.py",
        "tool/gatekeeper/providers/ibkr.py",
        "tool/*hist*.py",
        "tool/check_ibkr_acc.py",
        "tool/sell_all_shares.py",
        "tool/sync_ibkr_realtime.py",
        "tool/testvix.py",
        "tool/smoke/test_ib_paper_chain_probe.py",
        "scripts/premarket*.py",
        "scripts/scan_top_gainers.py",
        "scripts/load_historical_data.py",
        "scripts/ai_trader/research/eod_reconcile_report.py",
        "scripts/start_system_one_click.sh",
    ),
    "direct_ib_connect_async": (
        "services/exec/adapters/ib_adapter.py",
        "services/ibkr/ib_connection_manager.py",
        "services/ibkr/ib_gateway_recovery_coordinator.py",
        "services/market/ib_market_data.py",
        "tool/ops/broker_truth_convergence.py",
        "tool/cleanup_strategy_residuals.py",
        "tool/*hist*.py",
        "scripts/load_historical_data.py",
        "scripts/ai_trader/research/eod_reconcile_report.py",
        "scripts/start_system_one_click.sh",
    ),
    "broker_order_mutation": (
        "services/exec/adapters/ib_adapter.py",
        "services/maintenance/eod_cleanup.py",
        "tool/cleanup_strategy_residuals.py",
        "tool/sell_all_shares.py",
        "tool/smoke/test_index_super_win_restart_replay.py",
        "scripts/premarket*.py",
    ),
    "order_ref_prefix_owner_heuristic": (
        "services/exec/models.py",
        "services/exec/safety_kernel.py",
        "services/orders/truth.py",
        "services/reports/daily_trade_summary_service.py",
        "strategies/qqq_intraday_strategy.py",
        "strategies/index_super_win*.py",
        "strategies/stocks_kernel/**/*.py",
        "tool/check_ibkr_acc.py",
        "tool/report/build_paper_shadow_soak_gate_report.py",
        "tool/report/build_trading_system_audit_gate_report.py",
        "scripts/ai_trader/research/eod_reconcile_report.py",
    ),
    "symbol_only_attribution": (
        "services/exec/models.py",
        "tool/report/build_paper_shadow_soak_gate_report.py",
        "tool/report/build_trading_system_audit_gate_report.py",
    ),
    "symbol_only_flatten": (
        "tool/report/build_trading_system_audit_gate_report.py",
    ),
    "symbol_only_cancel": (
        "tool/report/build_trading_system_audit_gate_report.py",
    ),
    "auto_reconciler_mutation": (
        "tool/report/build_paper_shadow_soak_gate_report.py",
        "tool/report/build_trading_system_audit_gate_report.py",
    ),
    "broker_aggregate_overwrite": (
        "services/reconciliation/position_reconciliation.py",
        "services/reconciliation/position_sync.py",
        "services/production/position_safety_supervisor.py",
        "tool/report/build_paper_shadow_soak_gate_report.py",
        "tool/report/build_trading_system_audit_gate_report.py",
    ),
    "db_projection_used_as_truth": (
        "services/production/broker_truth_snapshot_service.py",
        "tool/report/build_trading_system_audit_gate_report.py",
    ),
}


def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(tz=ET)
    return now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(ET) if value.tzinfo else value.replace(tzinfo=ET)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone(ET) if parsed.tzinfo else parsed.replace(tzinfo=ET)
    except Exception:
        return None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _load_jsonl_last(path: Path) -> dict[str, Any] | None:
    last: dict[str, Any] | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            last = payload
    return last


def _generated_at(payload: dict[str, Any] | None) -> datetime | None:
    if not payload:
        return None
    for key in ("generated_at", "timestamp", "ts", "created_at"):
        parsed = _parse_ts(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _iter_scan_files(repo_root: Path, scan_dirs: Iterable[str]) -> Iterable[Path]:
    for dirname in scan_dirs:
        base = repo_root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".sh"}:
                yield path


def run_static_scan(
    *,
    repo_root: str | Path = ".",
    scan_dirs: Iterable[str] = DEFAULT_SCAN_DIRS,
    allowlist: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    allowed = allowlist or DEFAULT_ALLOWLIST
    findings: list[dict[str, Any]] = []
    allowed_findings: list[dict[str, Any]] = []
    files_scanned = 0

    for path in _iter_scan_files(root, scan_dirs):
        files_scanned += 1
        rel_path = _rel(path, root)
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for rule in SCAN_RULES:
                if not rule.pattern.search(line):
                    continue
                item = {
                    "risk": rule.risk,
                    "severity": rule.severity,
                    "path": rel_path,
                    "line": lineno,
                    "text": stripped[:240],
                }
                if _matches_any(rel_path, allowed.get(rule.risk, ())):
                    item["allowlisted"] = True
                    allowed_findings.append(item)
                else:
                    item["allowlisted"] = False
                    findings.append(item)

    return {
        "verdict": "PASS" if not findings else "FAIL",
        "files_scanned": files_scanned,
        "findings": findings,
        "allowlisted_findings": allowed_findings[:200],
        "allowlisted_finding_count": len(allowed_findings),
    }


def _latest_path(root: Path, patterns: Iterable[str]) -> Path | None:
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(path for path in root.glob(pattern) if path.is_file())
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _artifact_defs(repo_root: Path) -> dict[str, tuple[Path | None, int, bool]]:
    readiness = repo_root / "runtime" / "readiness"
    return {
        "production_state": (
            _latest_path(repo_root, ("runtime/readiness/production_state/production_state_latest.json", "runtime/readiness/production_state_latest.json")),
            90,
            True,
        ),
        "broker_truth": (
            _latest_path(
                repo_root,
                (
                    "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json",
                    "runtime/readiness/broker_truth_snapshot_latest.json",
                    "runtime/readiness/paper_broker_truth_snapshot_latest.json",
                    "runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json",
                ),
            ),
            90,
            True,
        ),
        "position_safety": (
            _latest_path(repo_root, ("runtime/readiness/position_safety/position_safety_latest.json", "runtime/readiness/position_safety_latest.json")),
            90,
            True,
        ),
        "runtime_version": (
            _latest_path(repo_root, ("runtime/readiness/runtime_version/runtime_version_latest.json", "runtime/readiness/runtime_version_latest.json")),
            90,
            True,
        ),
        "strategy_tradability": (
            _latest_path(repo_root, ("runtime/readiness/strategy_tradability/*.jsonl", "runtime/readiness/strategy_tradability_*.jsonl")),
            90,
            True,
        ),
        "strategy_surface": (_latest_path(repo_root, ("reports/strategy_surface/live_strategy_surface_report_*.json",)), 24 * 60 * 60, True),
        "approved_to_fill_conversion": (
            repo_root / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
            24 * 60 * 60,
            True,
        ),
        "vegas_dlb_edge_measurement": (
            repo_root / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
            24 * 60 * 60,
            True,
        ),
        "paper_shadow_soak_gate": (
            repo_root / "reports" / "paper_shadow_soak_gate" / "paper_shadow_soak_gate_latest.json",
            24 * 60 * 60,
            False,
        ),
    }


def _artifact_payload(path: Path) -> dict[str, Any] | None:
    if path.suffix == ".jsonl":
        return _load_jsonl_last(path)
    return _load_json(path)


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _text_blob(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    return " ".join(str(item).lower() for item in _walk(payload))


def audit_runtime_artifacts(
    *,
    repo_root: str | Path = ".",
    now: datetime | None = None,
    require_optional_soak: bool = False,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    now_et = _now(now)
    artifacts: dict[str, Any] = {}
    blockers: list[str] = []
    warnings: list[str] = []

    for name, (path, ttl_sec, required) in _artifact_defs(root).items():
        required = required or (name == "paper_shadow_soak_gate" and require_optional_soak)
        if path is None or not path.exists():
            status = {"status": "MISSING", "exists": False, "fresh": False, "path": str(path) if path else None, "ttl_sec": ttl_sec}
            if required:
                blockers.append(f"missing_artifact:{name}")
            else:
                warnings.append(f"missing_optional_artifact:{name}")
            artifacts[name] = status
            continue
        payload = _artifact_payload(path)
        ts = _generated_at(payload)
        age_sec = None if ts is None else max(0.0, (now_et - ts).total_seconds())
        fresh = bool(ts and age_sec is not None and age_sec <= ttl_sec)
        status = {
            "status": "PASS" if fresh else "FAIL",
            "exists": True,
            "fresh": fresh,
            "age_sec": None if age_sec is None else round(age_sec, 3),
            "ttl_sec": ttl_sec,
            "generated_at": ts.isoformat() if ts else None,
            "path": _rel(path, root),
        }
        artifacts[name] = status
        if not fresh and required:
            blockers.append(f"stale_or_invalid_artifact:{name}")

        blob = _text_blob(payload)
        if name == "broker_truth":
            source = str((payload or {}).get("source") or "").lower()
            backed = (payload or {}).get("broker_truth_backed")
            if backed is not True and not any(token in source for token in ("paper", "shadow", "broker")):
                blockers.append("broker_truth_source_not_explicit")
        elif name == "position_safety":
            if "lookup_failed" in blob and "lookup_failed true" in blob.replace(":", " "):
                blockers.append("position_safety_lookup_failed")
        elif name == "production_state":
            if not payload or not any(key in payload for key in ("summary", "buckets", "state", "go_no_go", "verdict")):
                blockers.append("production_state_not_explainable")
        elif name == "strategy_tradability":
            if "entry_failure" in blob and any(token in blob for token in ("idle", "offsession", "off_session")):
                warnings.append("strategy_tradability_review_idle_offsession_entry_failure")
        elif name == "strategy_surface":
            if any(token in blob for token in ("legacy/direct/private", "legacy direct private")) and "full production" in blob:
                blockers.append("strategy_surface_legacy_private_marked_full_production")

    if blockers:
        verdict = "INSUFFICIENT_DATA" if all(item.startswith("missing_artifact:") for item in blockers) else "FAIL"
    else:
        verdict = "PASS"
    return {"verdict": verdict, "artifacts": artifacts, "blockers": blockers, "warnings": warnings}


def _normalize_command_result(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or "unnamed")
    rc = item.get("returncode", item.get("rc", 1))
    try:
        returncode = int(rc)
    except Exception:
        returncode = 1
    status = str(item.get("status") or ("PASS" if returncode == 0 else "FAIL")).upper()
    if status not in VERDICTS and status not in {"SKIPPED"}:
        status = "FAIL"
    return {
        "name": name,
        "command": item.get("command"),
        "status": status,
        "returncode": returncode,
        "summary": item.get("summary") or "",
        "duration_sec": item.get("duration_sec"),
    }


def load_command_results(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("commands", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("command results must be a list or {'commands': [...]}")
    return [_normalize_command_result(row) for row in rows if isinstance(row, dict)]


def _command_map(command_results: list[dict[str, Any]]) -> dict[str, str]:
    return {str(item["name"]): str(item["status"]) for item in command_results}


def _is_fresh(payload: dict[str, Any] | None, *, now: datetime, ttl_sec: int) -> tuple[bool, float | None, str | None]:
    ts = _generated_at(payload)
    if ts is None:
        return False, None, None
    age_sec = max(0.0, (now - ts).total_seconds())
    return age_sec <= ttl_sec, round(age_sec, 3), ts.isoformat()


def _window_matches_day(payload: dict[str, Any], day: str | None) -> bool:
    if not day:
        return True
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    generated = _generated_at(payload)
    values = [
        payload.get("day"),
        payload.get("report_day"),
        window.get("start"),
        window.get("end"),
        generated.strftime("%Y%m%d") if generated else None,
    ]
    normalized = {str(value).replace("-", "") for value in values if value is not None}
    return day in normalized


def _diagnostic_status(
    repo_root: Path,
    name: str,
    *,
    now: datetime,
    day: str | None,
    min_sample_count: int = PRE_CANARY_MIN_SAMPLE_COUNT,
) -> dict[str, Any]:
    path_by_name = {
        "strategy_execution_reliability": repo_root / "reports" / "strategy_execution_reliability" / "strategy_execution_reliability_latest.json",
        "approved_to_fill_conversion": repo_root / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
        "vegas_dlb_edge_measurement": repo_root / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
        "paper_shadow_soak_gate": repo_root / "reports" / "paper_shadow_soak_gate" / "paper_shadow_soak_gate_latest.json",
    }
    path = path_by_name[name]
    payload = _load_json(path)
    if payload is None:
        return {"status": "MISSING", "path": _rel(path, repo_root)}
    fresh, age_sec, generated_at = _is_fresh(payload, now=now, ttl_sec=DIAGNOSTIC_TTL_SEC)
    if not fresh:
        return {
            "status": "INSUFFICIENT_DATA",
            "path": _rel(path, repo_root),
            "reason": "stale_or_missing_generated_at",
            "age_sec": age_sec,
            "generated_at": generated_at,
        }
    if not _window_matches_day(payload, day):
        return {
            "status": "INSUFFICIENT_DATA",
            "path": _rel(path, repo_root),
            "reason": "window_day_mismatch",
            "generated_at": generated_at,
            "window": payload.get("window"),
        }
    classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
    if name == "strategy_execution_reliability":
        primary = classification.get("primary_bottleneck")
        ok_values = {"NO_BOTTLENECK"}
    elif name == "approved_to_fill_conversion":
        primary = classification.get("primary_conversion_loss")
        ok_values = {"NO_BOTTLENECK"}
    elif name == "vegas_dlb_edge_measurement":
        primary = classification.get("primary_rejection_driver")
        ok_values = {"NO_BOTTLENECK"}
    else:
        primary = payload.get("verdict")
        ok_values = {"PASS"}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
        required_summary = ("artifact_freshness_pass", "oms_invariants_pass", "no_live_leak")
        bad_summary = [key for key in required_summary if summary.get(key) is not True]
        if blockers or bad_summary:
            return {
                "status": "FAIL",
                "path": _rel(path, repo_root),
                "primary": primary,
                "reason": "soak_gate_structural_failure",
                "blockers": blockers,
                "bad_summary": bad_summary,
                "generated_at": generated_at,
            }
    sample_count = payload.get("sample_count")
    if name != "paper_shadow_soak_gate":
        try:
            sample_count_int = int(sample_count)
        except Exception:
            sample_count_int = 0
        if sample_count_int < int(min_sample_count):
            return {
                "status": "INSUFFICIENT_DATA",
                "path": _rel(path, repo_root),
                "primary": primary,
                "sample_count": sample_count,
                "reason": "insufficient_sample_count",
                "generated_at": generated_at,
            }
    status = "PASS" if primary in ok_values else ("INSUFFICIENT_DATA" if primary == "INSUFFICIENT_SAMPLE" else "FAIL")
    return {
        "status": status,
        "path": _rel(path, repo_root),
        "primary": primary,
        "sample_count": sample_count,
        "blockers": payload.get("blockers") or [],
        "age_sec": age_sec,
        "generated_at": generated_at,
    }


def _oms_invariants_from_soak(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "reports" / "paper_shadow_soak_gate" / "paper_shadow_soak_gate_latest.json"
    payload = _load_json(path)
    if not payload:
        return {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    raw = payload.get("oms_invariants") if isinstance(payload.get("oms_invariants"), dict) else {}
    return {
        "owner_identity": bool(summary.get("owner_identity_pass")),
        "same_symbol_isolation": raw.get("symbol_only_attribution_count", 0) == 0,
        "durable_cancel": "unknown_owner_auto_cancel" not in set(payload.get("blockers") or []),
        "stop_loss_owner_binding": "missing_owner_identity" not in set(payload.get("blockers") or []),
        "reconnect_rebind": None,
        "manual_broker_adjudication": raw.get("manual_review_count", 0) >= raw.get("unknown_owner_count", 0),
        "oms_invariants_pass": bool(summary.get("oms_invariants_pass")),
        "paper_shadow_intent_count": raw.get("paper_shadow_intent_count"),
        "source": _rel(path, repo_root),
    }


def _build_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Trading System Audit Gate - {str(report.get('generated_at') or '')[:10]}",
        "",
        f"Mode: `{report.get('mode')}`",
        f"Verdict: **{report.get('verdict')}**",
        "",
        "## Summary",
        "",
        "| Layer | Verdict |",
        "|---|---|",
        f"| No-live guard | `{report.get('no_live_guard')}` |",
        f"| Static scan | `{(report.get('static_scan') or {}).get('verdict')}` |",
        f"| Tests | `{report.get('test_verdict')}` |",
        f"| Runtime artifacts | `{(report.get('runtime_artifacts') or {}).get('verdict')}` |",
        f"| Paper/shadow diagnostics | `{(report.get('paper_shadow') or {}).get('verdict')}` |",
        "",
        "## Blockers",
        "",
    ]
    blockers = report.get("blockers") or []
    lines.extend([f"- `{item}`" for item in blockers] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    warnings = report.get("warnings") or []
    lines.extend([f"- `{item}`" for item in warnings] or ["- none"])
    lines.extend(["", "## Commands", "", "| Name | Status | Summary |", "|---|---|---|"])
    for item in report.get("commands") or []:
        lines.append(f"| `{item.get('name')}` | `{item.get('status')}` | {str(item.get('summary') or '').replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def build_audit_gate_report(
    *,
    repo_root: str | Path = ".",
    mode: str = "quick",
    command_results: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    day: str | None = None,
    write_outputs: bool = True,
) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    root = Path(repo_root).resolve()
    now_et = _now(now)
    report_day = day or now_et.strftime("%Y%m%d")
    commands = [_normalize_command_result(item) for item in (command_results or [])]
    cmd_status = _command_map(commands)

    static_scan = run_static_scan(repo_root=root)
    artifact_required = mode in {"full", "pre_canary"}
    runtime_artifacts = (
        audit_runtime_artifacts(repo_root=root, now=now_et, require_optional_soak=(mode == "pre_canary"))
        if artifact_required
        else {"verdict": "SKIPPED", "artifacts": {}, "blockers": [], "warnings": []}
    )

    diagnostics: dict[str, Any] = {}
    paper_shadow_verdict = "SKIPPED"
    if mode == "pre_canary":
        diagnostics = {
            name: _diagnostic_status(root, name, now=now_et, day=report_day)
            for name in (
                "strategy_execution_reliability",
                "approved_to_fill_conversion",
                "vegas_dlb_edge_measurement",
                "paper_shadow_soak_gate",
            )
        }
        if any(item["status"] == "FAIL" for item in diagnostics.values()):
            paper_shadow_verdict = "FAIL"
        elif any(item["status"] in {"MISSING", "INSUFFICIENT_DATA"} for item in diagnostics.values()):
            paper_shadow_verdict = "INSUFFICIENT_DATA"
        else:
            paper_shadow_verdict = "PASS"

    blockers: list[str] = []
    warnings: list[str] = []
    no_live_guard = cmd_status.get("no_live_guard", "NOT_RUN")
    if no_live_guard != "PASS":
        blockers.append("no_live_guard_not_passed")
    missing_required_commands = [name for name in REQUIRED_COMMANDS_BY_MODE[mode] if cmd_status.get(name) != "PASS"]
    for name in missing_required_commands:
        if name not in cmd_status:
            blockers.append(f"missing_required_command:{name}")
    if static_scan["verdict"] != "PASS":
        blockers.append("static_scan_critical_findings")
    blockers.extend(runtime_artifacts.get("blockers") or [])
    warnings.extend(runtime_artifacts.get("warnings") or [])

    for item in commands:
        if item["status"] == "FAIL":
            blockers.append(f"command_failed:{item['name']}")
        elif item["status"] == "INSUFFICIENT_DATA":
            blockers.append(f"command_insufficient_data:{item['name']}")
        elif item["status"] == "SKIPPED":
            warnings.append(f"command_skipped:{item['name']}")

    if mode == "pre_canary":
        for name, item in diagnostics.items():
            if item["status"] == "FAIL":
                blockers.append(f"diagnostic_failed:{name}")
            elif item["status"] in {"MISSING", "INSUFFICIENT_DATA"}:
                blockers.append(f"diagnostic_insufficient_data:{name}")

    if any(
        str(item).startswith(("missing_artifact:", "missing_required_command:")) or "insufficient_data" in str(item)
        for item in blockers
    ) and not any(
        str(item).startswith(("command_failed:", "diagnostic_failed:")) or item == "static_scan_critical_findings" for item in blockers
    ):
        verdict = "INSUFFICIENT_DATA"
    elif blockers:
        verdict = "FAIL"
    else:
        verdict = "PASS"

    report = {
        "generated_at": now_et.isoformat(),
        "mode": mode,
        "verdict": verdict,
        "no_live_guard": no_live_guard,
        "static_scan": static_scan,
        "test_verdict": "FAIL"
        if any(item["status"] == "FAIL" for item in commands)
        else ("INSUFFICIENT_DATA" if missing_required_commands or any(item["status"] == "INSUFFICIENT_DATA" for item in commands) else "PASS"),
        "test_results": {item["name"]: item["summary"] or item["status"] for item in commands},
        "runtime_artifacts": runtime_artifacts,
        "oms_invariants": _oms_invariants_from_soak(root),
        "paper_shadow": {
            "verdict": paper_shadow_verdict,
            "diagnostics": diagnostics,
        },
        "commands": commands,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "approved_scope": ["manual_canary_review"] if verdict == "PASS" and mode == "pre_canary" else [],
        "not_approved_scope": ["live_trading", "auto_canary_launch"],
    }

    if write_outputs:
        out_dir = root / "reports" / "audit_gate"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "audit_gate_latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        docs_dir = root / "docs" / "reports"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / f"audit_gate_{report_day}.md").write_text(_build_markdown(report), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the layered trading system audit gate report")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--mode", default="quick", choices=sorted(MODES))
    parser.add_argument("--command-results-json", default=None)
    parser.add_argument("--day", default=None)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    report = build_audit_gate_report(
        repo_root=args.repo_root,
        mode=args.mode,
        command_results=load_command_results(args.command_results_json),
        day=args.day,
        write_outputs=not args.no_write,
    )
    print(report["verdict"])
    return 0 if report["verdict"] == "PASS" else (3 if report["verdict"] == "INSUFFICIENT_DATA" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
