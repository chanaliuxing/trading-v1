from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def build_paper_candidates(
    *,
    shadow_candidates_dir: str | Path,
    output_dir: str | Path,
) -> Dict[str, Any]:
    src = Path(shadow_candidates_dir)
    out = Path(output_dir) / "paper_candidates"
    out.mkdir(parents=True, exist_ok=True)

    manifests: list[str] = []
    if not src.exists():
        return {"paper_candidates_dir": str(out), "paper_manifests": manifests}

    for candidate_dir in sorted(src.iterdir()):
        if not candidate_dir.is_dir():
            continue
        candidate_manifest_path = candidate_dir / "candidate_manifest.json"
        allocation_ref_path = candidate_dir / "allocation_ref.json"
        execution_constraints_path = candidate_dir / "execution_constraints.json"
        if not candidate_manifest_path.exists():
            continue

        candidate_manifest = _as_dict(json.loads(candidate_manifest_path.read_text(encoding="utf-8")))
        allocation_ref = (
            _as_dict(json.loads(allocation_ref_path.read_text(encoding="utf-8")))
            if allocation_ref_path.exists()
            else {}
        )
        execution_constraints = (
            _as_dict(json.loads(execution_constraints_path.read_text(encoding="utf-8")))
            if execution_constraints_path.exists()
            else {}
        )

        candidate_id = str(candidate_dir.name)
        paper_dir = out / candidate_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        paper_manifest = {
            "candidate_id": candidate_id,
            "experiment_id": str(candidate_manifest.get("experiment_id") or candidate_id),
            "promotion_target": str(candidate_manifest.get("promotion_target") or "paper_candidate"),
            "allocation": allocation_ref,
            "execution_constraints": execution_constraints,
            "entrypoint": {"mode": "paper", "runner": "shadow_runner_v1"},
        }
        paper_path = paper_dir / "paper_manifest.json"
        paper_path.write_text(json.dumps(paper_manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        manifests.append(str(paper_path))

    return {"paper_candidates_dir": str(out), "paper_manifests": manifests}
