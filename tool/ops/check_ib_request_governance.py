#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]

IB_REQUEST_CALLS = {
    "qualifyContractsAsync",
    "reqMktData",
    "cancelMktData",
    "reqSecDefOptParamsAsync",
    "reqHistoricalDataAsync",
}

APPROVED_BROKER_MODULES = {
    "services/ibkr/ib_connection_manager.py",
    "services/ibkr/ib_request_governor.py",
    "services/exec/adapters/ib_adapter.py",
    "services/market/ib_market_data.py",
    "services/market/options_data.py",
}

IGNORED_PATH_PREFIXES = (
    "tests/",
    "tools/test_",
)


def _repo_rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _iter_python_files(root: Path) -> list[Path]:
    scopes = ["services", "strategies", "run_system.py"]
    files: list[Path] = []
    for scope in scopes:
        p = root / scope
        if p.is_file() and p.suffix == ".py":
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
    return sorted(set(files))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return str(node.attr)
    if isinstance(node, ast.Name):
        return str(node.id)
    return ""


def _find_ib_request_calls(path: Path) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except Exception as exc:
        return [{"line": 0, "call": "parse_error", "error": str(exc)}]

    calls: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in IB_REQUEST_CALLS:
            calls.append({"line": int(getattr(node, "lineno", 0) or 0), "call": name})
    return calls


def build_report(*, repo_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    violations: list[dict[str, Any]] = []
    governed: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []

    for path in _iter_python_files(root):
        rel = _repo_rel(path, root)
        if rel.startswith(IGNORED_PATH_PREFIXES):
            continue
        calls = _find_ib_request_calls(path)
        calls = [c for c in calls if c.get("call") in IB_REQUEST_CALLS]
        if not calls:
            continue

        if rel in APPROVED_BROKER_MODULES:
            approved.append({"path": rel, "calls": calls})
            continue

        text = path.read_text(encoding="utf-8", errors="ignore")
        has_governor = "allow_market_request" in text and "record_market_request_result" in text
        item = {
            "path": rel,
            "calls": calls,
            "has_governor": bool(has_governor),
        }
        if has_governor:
            governed.append(item)
        else:
            violations.append(item)

    return {
        "status": "PASS" if not violations else "FAIL",
        "violations": violations,
        "governed_callers": governed,
        "approved_broker_modules": approved,
        "policy": {
            "approved_broker_modules": sorted(APPROVED_BROKER_MODULES),
            "require_governor_for_non_approved_runtime_callers": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check direct IB API request calls are broker-approved or governed.")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    report = build_report(repo_root=args.repo_root or None)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        out = Path(args.output)
        if not out.is_absolute():
            out = (Path(args.repo_root).resolve() if args.repo_root else REPO_ROOT) / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
