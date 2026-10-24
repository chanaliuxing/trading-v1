
import json
import yaml
import pytest
from pathlib import Path
from scripts.ai_trader.config import alpha_config_patcher

@pytest.fixture
def alpha_config_files(tmp_path):
    """
    Creates temporary alpha_config and label_profiles.yml files.
    """
    # Create dummy alpha_config_enhanced.json
    alpha_config_content = {"label_profile": "initial_profile"}
    (tmp_path / "alpha_config_enhanced.json").write_text(json.dumps(alpha_config_content))

    # Create dummy label_profiles.yml
    label_profiles_content = {
        "profiles": {
            "intraday_reg_5m": {"task": "reg"},
            "intraday_reg_10m": {"task": "regression"}, # Test different case
            "intraday_cls_5m": {"task": "binary"},
            "another_reg_profile": {} # Should default to reg
        }
    }
    (tmp_path / "label_profiles.yml").write_text(yaml.dump(label_profiles_content))
    
    return {
        "alpha_config": "alpha_config_enhanced.json",
        "labels_config": "label_profiles.yml"
    }

@pytest.fixture(autouse=True)
def patch_alpha_constants(monkeypatch, tmp_path):
    """
    Monkeypatches the constants in alpha_config_patcher to use the test setup.
    """
    monkeypatch.setattr(alpha_config_patcher, 'ROOT_DIR', tmp_path)
    monkeypatch.setattr(alpha_config_patcher, 'ALLOWED_FILES', {"alpha_config_enhanced.json"})
    monkeypatch.setattr(alpha_config_patcher, 'BACKUP_DIR', tmp_path / "backups")

def test_alpha_config_patcher_only_allows_whitelisted_files_and_key(alpha_config_files):
    """
    Tests that the patcher rejects non-whitelisted files and keys.
    """
    # 1. Reject non-whitelisted file
    result_bad_file = alpha_config_patcher.update(
        file_path="another_config.json",
        key="label_profile",
        value="intraday_reg_5m"
    )
    assert result_bad_file["ok"] is False
    assert "not in whitelist" in result_bad_file["error"]

    # 2. Reject non-whitelisted key
    result_bad_key = alpha_config_patcher.update(
        file_path=alpha_config_files["alpha_config"],
        key="some_other_key",
        value="some_value"
    )
    assert result_bad_key["ok"] is False
    assert "not supported" in result_bad_key["error"]

def test_alpha_config_patcher_label_profile_from_label_profiles_yml(alpha_config_files, tmp_path):
    """
    Tests that the patcher correctly updates the label_profile based on the
    allowed regression profiles from label_profiles.yml.
    """
    alpha_config_path = tmp_path / alpha_config_files["alpha_config"]

    # 1. Valid update: should succeed
    result_ok = alpha_config_patcher.update(
        file_path=alpha_config_files["alpha_config"],
        key="label_profile",
        value="intraday_reg_5m" # Use a profile with the exact task "reg"
    )
    assert result_ok["ok"] is True
    assert result_ok["old_value"] == "initial_profile"
    assert result_ok["new_value"] == "intraday_reg_5m"

    # Check backup was created
    backup_dir = tmp_path / "backups"
    assert len(list(backup_dir.glob("*.json"))) == 1

    # Check file was modified
    with alpha_config_path.open("r") as f:
        data = json.load(f)
    assert data["label_profile"] == "intraday_reg_5m"

    # 2. Invalid profile (not a regression task): should fail
    result_bad_task = alpha_config_patcher.update(
        file_path=alpha_config_files["alpha_config"],
        key="label_profile",
        value="intraday_cls_5m"
    )
    assert result_bad_task["ok"] is False
    assert "not in allowed set" in result_bad_task["error"]

    # 3. Non-existent profile: should fail
    result_non_existent = alpha_config_patcher.update(
        file_path=alpha_config_files["alpha_config"],
        key="label_profile",
        value="does_not_exist"
    )
    assert result_non_existent["ok"] is False
    assert "not in allowed set" in result_non_existent["error"]
    
    # Verify file was not changed from last valid update
    with alpha_config_path.open("r") as f:
        data_after_invalid = json.load(f)
    assert data_after_invalid["label_profile"] == "intraday_reg_5m"
