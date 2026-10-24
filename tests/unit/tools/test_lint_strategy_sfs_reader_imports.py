from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "tools" / "lint_strategy_sfs_reader_imports.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("lint_strategy_sfs_reader_imports", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_sfs_reader_baseline_does_not_grow():
    module = _load_module()

    assert module.find_violations(REPO_ROOT) == []


def test_new_strategy_sfs_reader_reference_is_rejected(tmp_path):
    module = _load_module()
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "new_strategy.py").write_text(
        "from libs.dal.sfs_facade import SFS1sReader\n",
        encoding="utf-8",
    )

    violations = module.find_violations(tmp_path)

    assert violations == [
        "strategies/new_strategy.py: SFS1sReader references increased from 0 to 1"
    ]
