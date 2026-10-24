from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from libs.logger import get_logger

log = get_logger(__name__)


def _load_registry(path: str = "config/skill_registry.json") -> Dict[str, Any]:
    data = json.loads(Path(path).read_text())
    return data.get("skills", {}) if isinstance(data, dict) else {}


def _hash_output(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _normalize_output(out: Any) -> Any:
    # Normalize pandas outputs for hashing
    try:
        if isinstance(out, pd.Series):
            return out.tolist()
        if isinstance(out, pd.DataFrame):
            return out.to_dict(orient="list")
    except Exception:
        pass
    return out


def run_pure_skill(skill_name: str, registry_path: str = "config/skill_registry.json") -> Dict[str, Any]:
    skills = _load_registry(registry_path)
    spec = skills.get(skill_name)
    if not spec:
        raise ValueError(f"skill not found: {skill_name}")
    if spec.get("type") != "PURE":
        raise ValueError("golden_runner only supports PURE skills")

    fixture_path = Path("tests/fixtures/golden_io.json")
    if not fixture_path.exists():
        raise FileNotFoundError("tests/fixtures/golden_io.json missing")

    fixtures = json.loads(fixture_path.read_text())
    cases = fixtures.get(skill_name, [])
    if not cases:
        raise ValueError(f"No fixtures for {skill_name}")

    # Dynamically import function if specified
    file_path = spec.get("file_path")
    module_path = file_path.replace("/", ".").removesuffix(".py")
    func_name = cases[0].get("func") or "slow_fib"
    mod = __import__(module_path, fromlist=[func_name])
    fn = getattr(mod, func_name)

    results = []
    for case in cases:
        args = case.get("args", [])
        kwargs = case.get("kwargs", {})
        # Optional dataframe input
        if "dataframe" in case:
            df_def = case["dataframe"]
            df = pd.DataFrame(df_def.get("data", []), columns=df_def.get("columns"))
            args = [df] + args
        expected_hash = case.get("hash")
        out = fn(*args, **kwargs)
        out_norm = _normalize_output(out)
        out_hash = _hash_output(out_norm)
        ok = expected_hash is None or expected_hash == out_hash
        results.append({"case": case, "out_hash": out_hash, "ok": ok})
    all_ok = all(r["ok"] for r in results)
    return {"ok": all_ok, "results": results}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True)
    args = ap.parse_args()
    res = run_pure_skill(args.skill)
    print(json.dumps(res, indent=2))
    if not res["ok"]:
        raise SystemExit(1)
