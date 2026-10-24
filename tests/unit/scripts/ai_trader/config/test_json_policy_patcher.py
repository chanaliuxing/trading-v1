
import json
import pytest
from pathlib import Path
from scripts.ai_trader.config import json_policy_patcher

@pytest.fixture
def policy_files(tmp_path):
    """
    Creates temporary, valid policy files in a temporary directory
    and monkeypatches the patcher's constants to use them.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Create dummy files
    ml_policy_content = {"label": {"horizon_sec": 60, "clip_abs": 0.1}}
    (tmp_path / "ml_policy.json").write_text(json.dumps(ml_policy_content))

    gating_policies_content = {"defaults": {"conf_floor": 0.5}}
    (config_dir / "gating_policies.json").write_text(json.dumps(gating_policies_content))
    
    # Return a dictionary of relative paths for the patcher
    return {
        "ml_policy": "ml_policy.json",
        "gating_policies": "config/gating_policies.json",
    }

@pytest.fixture(autouse=True)
def patch_constants(monkeypatch, tmp_path):
    """
    Monkeypatches the constants in json_policy_patcher to point to our
    temporary test directory and use test-specific bounds.
    """
    # Patch ROOT_DIR to be the temporary directory
    monkeypatch.setattr(json_policy_patcher, 'ROOT_DIR', tmp_path)

    # Patch ALLOWED_FILES
    monkeypatch.setattr(json_policy_patcher, 'ALLOWED_FILES', {
        "ml_policy.json",
        "config/gating_policies.json",
    })

    # Patch PARAM_BOUNDS
    monkeypatch.setattr(json_policy_patcher, 'PARAM_BOUNDS', {
        "ml_policy.json": {
            "label.horizon_sec": (30, 600, "Test Horizon"),
            "label.clip_abs": (0.01, 0.5, "Test Clip"),
        },
        "config/gating_policies.json": {
            "defaults.conf_floor": (0.1, 0.9, "Test Confidence Floor"),
        },
    })
    
    # Patch backup directory
    monkeypatch.setattr(json_policy_patcher, 'BACKUP_DIR', tmp_path / "backups")


def test_json_policy_patcher_bounds_and_whitelist(policy_files):
    """
    Tests the patcher's core validation logic:
    - A valid update should succeed.
    - An update to a non-whitelisted file should be rejected.
    - An update with an out-of-bounds value should be rejected.
    """
    # 1. Valid case: should succeed
    result_ok = json_policy_patcher.update(
        file_path=policy_files["ml_policy"],
        key="label.horizon_sec",
        value=120
    )
    assert result_ok["ok"] is True
    assert result_ok["new_value"] == 120

    # 2. Invalid file: should be rejected
    result_bad_file = json_policy_patcher.update(
        file_path="config/some_other_file.json",
        key="any.key",
        value=1
    )
    assert result_bad_file["ok"] is False
    assert "not in whitelist" in result_bad_file["error"]

    # 3. Out-of-bounds value: should be rejected
    result_out_of_bounds = json_policy_patcher.update(
        file_path=policy_files["ml_policy"],
        key="label.horizon_sec",
        value=9999  # Exceeds the max of 600
    )
    assert result_out_of_bounds["ok"] is False
    assert "must be in [30, 600]" in result_out_of_bounds["error"]

    # 4. Invalid key for a valid file: should be rejected
    result_bad_key = json_policy_patcher.update(
        file_path=policy_files["ml_policy"],
        key="label.non_existent_key",
        value=100
    )
    assert result_bad_key["ok"] is False
    assert "not in whitelist" in result_bad_key["error"]


def test_json_policy_patcher_ml_policy_fields(policy_files, tmp_path):
    """
    Tests updating a specific field in ml_policy.json, checking for
    success, backup creation, and file modification.
    """
    ml_policy_path = tmp_path / policy_files["ml_policy"]

    # 1. Valid update
    result = json_policy_patcher.update(
        file_path=policy_files["ml_policy"],
        key="label.clip_abs",
        value=0.25
    )
    assert result["ok"] is True
    assert result["old_value"] == 0.1
    assert result["new_value"] == 0.25
    
    # Check that backup was created
    backup_dir = tmp_path / "backups"
    backups = list(backup_dir.glob("ml_policy.*.json"))
    assert len(backups) == 1
    
    # Check that the file was actually modified
    with ml_policy_path.open("r") as f:
        data = json.load(f)
    assert data["label"]["clip_abs"] == 0.25

    # 2. Invalid (out-of-bounds) update
    result_invalid = json_policy_patcher.update(
        file_path=policy_files["ml_policy"],
        key="label.clip_abs",
        value=1.0  # Exceeds max of 0.5
    )
    assert result_invalid["ok"] is False
    assert "must be in [0.01, 0.5]" in result_invalid["error"]

    # Verify the file was not changed from the last valid state
    with ml_policy_path.open("r") as f:
        data_after_invalid = json.load(f)
    assert data_after_invalid["label"]["clip_abs"] == 0.25


def test_profit_guard_blocks_conf_floor_lowering(policy_files):
    """
    Tests the internal profit guard in `update` specifically for conf_floor.
    """
    gating_file = policy_files["gating_policies"]

    # 1. Attempt to lower conf_floor. This should be rejected by the guard.
    result_blocked = json_policy_patcher.update(
        file_path=gating_file,
        key="defaults.conf_floor",
        value=0.4  # Current is 0.5, so this is a decrease
    )
    assert result_blocked["ok"] is False
    assert "cannot be lowered" in result_blocked["error"]

    # 2. Attempt to raise conf_floor. This should be allowed.
    result_allowed = json_policy_patcher.update(
        file_path=gating_file,
        key="defaults.conf_floor",
        value=0.6  # Current is 0.5, so this is an increase
    )
    assert result_allowed["ok"] is True
    assert result_allowed["new_value"] == 0.6
