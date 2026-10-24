#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DIRECT_CALL_PATTERNS = {
    "ctx_submit_intent": ".submit_intent(",
    "execution_service": "ExecutionService",
    "ib_adapter": "IBAdapter",
    "ib_insync_adapter": "IBInsyncAdapter",
    "safety_kernel_place_order": ".place_order(",
    "safety_kernel_place_entry_order": ".place_entry_order(",
}


PLATFORM_INTERNAL_PREFIXES = (
    "services/exec/",
    "services/orders/",
    "services/ibkr/",
    "services/portfolio/",
    "services/reconciliation/",
    "services/risk/",
)


def _classify_hit(*, rel_path: str, target: str) -> tuple[str, str, str]:
    if rel_path.startswith("tests/") or rel_path.startswith("tool/"):
        return "not_production", "test_or_tool", "test/tool surface only"
    if rel_path == "run_system.py":
        return "wired", "platform_bootstrap", "production bootstrap wires UOM/ExecutionService"
    if any(rel_path.startswith(prefix) for prefix in PLATFORM_INTERNAL_PREFIXES):
        return "platform_internal", "platform_internal", "allowed inside broker/execution platform boundary"
    if target == "ctx_submit_intent":
        return "routed_by_pipeline", "low", "strategy intent API routes through DecisionPipeline -> UOM"
    return "pending", "high", "direct production caller must migrate behind UOM"


def _scan_file(path: Path, *, repo_root: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    rows: list[dict[str, Any]] = []
    rel_path = str(path.relative_to(repo_root))
    for name, needle in DIRECT_CALL_PATTERNS.items():
        if needle not in text:
            continue
        migration_status, risk, note = _classify_hit(rel_path=rel_path, target=name)
        rows.append(
            {
                "file": str(path),
                "relative_file": rel_path,
                "direct_call_target": name,
                "needle": needle,
                "migration_status": migration_status,
                "risk_level": risk,
                "note": note,
            }
        )
    return rows


def build_uom_caller_surface_report(repo_root: str | Path = ".") -> dict[str, Any]:
    root = Path(repo_root).resolve()
    scan_roots = [root / "strategies", root / "services", root / "run_system.py", root / "tool"]
    files: list[Path] = []
    for scan_root in scan_roots:
        if scan_root.is_file():
            files.append(scan_root)
        elif scan_root.exists():
            files.extend(path for path in scan_root.rglob("*.py") if path.is_file())
    rows: list[dict[str, Any]] = []
    for path in sorted(set(files)):
        if "__pycache__" in path.parts:
            continue
        rows.extend(_scan_file(path, repo_root=root))
    production_pending = [
        row for row in rows if row["migration_status"] == "pending"
    ]
    platform_internal = [row for row in rows if row["migration_status"] == "platform_internal"]
    routed_by_pipeline = [row for row in rows if row["migration_status"] == "routed_by_pipeline"]
    test_or_tool = [row for row in rows if row["migration_status"] == "not_production"]
    return {
        "repo_root": str(root),
        "summary": {
            "caller_count": len(rows),
            "production_pending_count": len(production_pending),
            "platform_internal_count": len(platform_internal),
            "routed_by_pipeline_count": len(routed_by_pipeline),
            "test_or_tool_count": len(test_or_tool),
        },
        "production_pending_files": sorted({row["file"] for row in production_pending}),
        "callers": rows,
    }


def evaluate_uom_caller_gate(report: dict[str, Any]) -> dict[str, Any]:
    pending = list(report.get("production_pending_files") or [])
    return {
        "ok": len(pending) == 0,
        "blocking_files": sorted(str(item) for item in pending),
        "summary": dict(report.get("summary") or {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build UOM direct-caller migration surface report")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    report = build_uom_caller_surface_report(args.repo_root)
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
        return 0
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
