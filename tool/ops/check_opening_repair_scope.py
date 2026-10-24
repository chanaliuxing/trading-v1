#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ALLOWED_PREFIXES = (
    "tool/ops/",
    "tests/unit/tool/",
    "docs/implementation/",
    "ops/launchd/",
)

ALLOWED_EXACT = {
    "scripts/run_system_autostart.sh",
}

FORBIDDEN_PREFIXES = (
    "strategies/",
    "services/",
    "infra/",
    "libs/",
    "ml/",
    "ai_brain/",
    "llm_trader/",
    "rl_core/",
    "data/",
    "features/",
    "labels/",
    "schema/",
)

FORBIDDEN_EXACT = {
    "run_system.py",
    "config.yml",
}

IGNORED_PREFIXES = (
    "reports/",
    "runtime/",
    "logs/",
)


def _git_changed_files(repo_root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    rows: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path_text = line[3:].strip()
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1].strip()
        if path_text:
            rows.append(path_text)
    return sorted(set(rows))


def _classify(path: str, *, extra_allowed: Iterable[str]) -> str:
    norm = str(path).strip().lstrip("./")
    if not norm:
        return "ignored"
    if norm in FORBIDDEN_EXACT or any(norm.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        return "forbidden"
    if norm in ALLOWED_EXACT or any(norm.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        return "allowed"
    if any(norm.startswith(prefix) for prefix in extra_allowed):
        return "allowed"
    if any(norm.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return "ignored"
    return "forbidden"


def check_opening_repair_scope(
    *,
    repo_root: str | Path | None = None,
    extra_allowed_prefixes: list[str] | None = None,
) -> dict[str, object]:
    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    extra_allowed = [str(item).strip().rstrip("/") + "/" for item in (extra_allowed_prefixes or []) if str(item).strip()]
    changed = _git_changed_files(root)
    allowed: list[str] = []
    forbidden: list[str] = []
    ignored: list[str] = []
    for path in changed:
        verdict = _classify(path, extra_allowed=extra_allowed)
        if verdict == "allowed":
            allowed.append(path)
        elif verdict == "forbidden":
            forbidden.append(path)
        else:
            ignored.append(path)
    ok = len(forbidden) == 0
    return {
        "repo_root": str(root),
        "ok": ok,
        "changed_files": changed,
        "allowed_files": allowed,
        "forbidden_files": forbidden,
        "ignored_files": ignored,
        "allowed_prefixes": list(ALLOWED_PREFIXES) + list(extra_allowed),
        "forbidden_prefixes": list(FORBIDDEN_PREFIXES),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate that opening fallback repairs stay within the safe file scope")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--allow-prefix", action="append", default=[])
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    payload = check_opening_repair_scope(
        repo_root=args.repo_root or None,
        extra_allowed_prefixes=list(args.allow_prefix or []),
    )
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = REPO_ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": payload["ok"], "forbidden_files": payload["forbidden_files"], "allowed_files": payload["allowed_files"]}, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
