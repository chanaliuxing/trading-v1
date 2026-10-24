#!/usr/bin/env python3
from __future__ import annotations

"""
Feature Apply (P0 skeleton)

Promotes candidate features to active in features_catalog.json (in-place update with backup).
Writes an apply audit file under runs/ai_trader/<date>/feature_apply.
"""

import argparse
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default="config/features_catalog.json")
    ap.add_argument("--out", default=None, help="Output dir (defaults to runs/ai_trader/<today>/feature_apply)")
    args = ap.parse_args()

    dstr = date.today().isoformat()
    out_dir = Path(args.out) if args.out else (Path("runs/ai_trader") / dstr / "feature_apply")
    out_dir.mkdir(parents=True, exist_ok=True)

    p = Path(args.catalog)
    if not p.exists():
        (out_dir / "apply.json").write_text(json.dumps({"ok": False, "error": "catalog_missing"}, indent=2), encoding="utf-8")
        print(json.dumps({"ok": False}))
        return

    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception as e:
        (out_dir / "apply.json").write_text(json.dumps({"ok": False, "error": f"parse:{e}"}, indent=2), encoding="utf-8")
        print(json.dumps({"ok": False}))
        return

    version = str(data.get("version") or "v0")
    active = data.get("active") or []
    cand = data.get("candidate") or []
    new_active = list(active)
    applied = 0
    for c in cand:
        if isinstance(c, dict) and c.get("name"):
            # de-dup by name
            if c["name"] not in [a.get("name") for a in new_active if isinstance(a, dict)]:
                new_active.append(c)
                applied += 1
    # Clear candidates after apply
    data["active"] = new_active
    data["candidate"] = []
    data["updated_at"] = datetime.utcnow().isoformat()

    # Backup and write back
    backup = p.with_suffix(p.suffix + f".{dstr}.bak")
    try:
        shutil.copyfile(p, backup)
    except Exception:
        pass
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    out = {"ok": True, "applied": applied, "version": version, "ts": datetime.utcnow().isoformat(), "backup": str(backup)}
    (out_dir / "apply.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out))


if __name__ == "__main__":
    main()

