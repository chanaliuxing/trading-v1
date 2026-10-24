from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"
DEFAULT_REPO_ROOT = "."

_NEW_FIELDS = ["policy_compatibility_observability", "recent_examples"]
_LEGACY_FIELDS = ["policy_compatibility", "recent_hits"]

_NEW_POLICY_RE = re.compile(r"(?<![A-Za-z0-9_])policy_compatibility_observability(?![A-Za-z0-9_])")
_LEGACY_POLICY_RE = re.compile(r"(?<![A-Za-z0-9_])policy_compatibility(?!_observability)(?![A-Za-z0-9_])")
_NEW_RECENT_RE = re.compile(r"(?<![A-Za-z0-9_])recent_examples(?![A-Za-z0-9_])")
_LEGACY_RECENT_RE = re.compile(r"(?<![A-Za-z0-9_])recent_hits(?![A-Za-z0-9_])")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reference_type(path: Path) -> str:
    parts = {p.lower() for p in path.parts}
    suffix = path.suffix.lower()
    if "tests" in parts:
        return "test"
    if "docs" in parts or suffix in {".md", ".rst"}:
        return "docs"
    return "runtime"


def _scan_file(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}

    new_fields: list[str] = []
    legacy_fields: list[str] = []
    if _NEW_POLICY_RE.search(text):
        new_fields.append("policy_compatibility_observability")
    if _NEW_RECENT_RE.search(text):
        new_fields.append("recent_examples")
    if _LEGACY_POLICY_RE.search(text):
        legacy_fields.append("policy_compatibility")
    if _LEGACY_RECENT_RE.search(text):
        legacy_fields.append("recent_hits")
    if not new_fields and not legacy_fields:
        return {}

    reference_type = _reference_type(path)
    consumer_mode = "legacy_only"
    if new_fields and legacy_fields:
        consumer_mode = "mixed"
    elif new_fields:
        consumer_mode = "new_only"

    is_legacy = bool(legacy_fields)
    migration_priority = "none"
    if reference_type == "runtime" and is_legacy:
        migration_priority = "high" if consumer_mode == "legacy_only" else "medium"

    return {
        "file_path": str(path),
        "reference_type": reference_type,
        "consumed_fields": sorted(set(new_fields + legacy_fields)),
        "new_fields": sorted(set(new_fields)),
        "legacy_fields": sorted(set(legacy_fields)),
        "is_legacy": is_legacy,
        "consumer_mode": consumer_mode,
        "migration_priority": migration_priority,
    }


def build_policy_observability_consumer_inventory(
    *,
    repo_root: str | Path = DEFAULT_REPO_ROOT,
) -> Dict[str, Any]:
    root = Path(repo_root)
    candidates: list[Path] = []
    for folder in ("scripts", "tests", "docs"):
        fpath = root / folder
        if not fpath.exists():
            continue
        candidates.extend(
            p
            for p in fpath.rglob("*")
            if p.is_file() and p.suffix.lower() in {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".rst"}
        )

    entries = []
    for path in sorted(candidates, key=lambda x: str(x)):
        row = _scan_file(path)
        if row:
            entries.append(row)

    runtime = [x for x in entries if x.get("reference_type") == "runtime"]
    runtime_legacy = [x for x in runtime if bool(x.get("is_legacy"))]
    runtime_new_only = [x for x in runtime if str(x.get("consumer_mode") or "") == "new_only"]
    docs_or_tests = [x for x in entries if x.get("reference_type") in {"docs", "test"}]
    docs_or_tests_legacy = [x for x in docs_or_tests if bool(x.get("is_legacy"))]

    return {
        "generated_at": _iso_now(),
        "root_path": str(root.resolve()),
        "summary": {
            "total_consumers": len(entries),
            "runtime_consumers": len(runtime),
            "runtime_legacy_consumers": len(runtime_legacy),
            "runtime_new_only_consumers": len(runtime_new_only),
            "docs_or_tests_consumers": len(docs_or_tests),
            "docs_or_tests_legacy_refs": len(docs_or_tests_legacy),
        },
        "entries": entries,
    }


def persist_policy_observability_consumer_inventory(
    *,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    repo_root: str | Path = DEFAULT_REPO_ROOT,
) -> Dict[str, Any]:
    payload = build_policy_observability_consumer_inventory(repo_root=repo_root)
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "policy_observability_consumers.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "policy_observability_consumers_json": str(path),
        "policy_observability_consumers": payload,
    }

