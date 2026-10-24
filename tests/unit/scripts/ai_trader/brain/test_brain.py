
import re
import pytest

# Import the constants and the internal validation function to be tested
from scripts.ai_trader.brain.brain import ML_RESULT_REVIEW_PROMPT
from scripts.ai_trader.config.json_policy_patcher import PARAM_BOUNDS as JSON_POLICY_PARAM_BOUNDS
from scripts.ai_trader.config.json_policy_patcher import _get_bounds as get_json_policy_bounds
from scripts.ai_trader.config.alpha_config_patcher import ALLOWED_FILES as ALPHA_CONFIG_ALLOWED_FILES

def test_ml_result_review_prompt_examples_are_valid_fix_config():
    """
    A lightweight test to verify that the hardcoded `fix_config` examples
    in the ML_RESULT_REVIEW_PROMPT are valid according to the whitelists
    in the respective patcher modules. This test now uses the internal
    `_get_bounds` function to correctly handle wildcard paths.
    """
    # Regex to find all example JSON objects for fix_config
    pattern = re.compile(r'{"path":\s*"([^"]+)",\s*"key":\s*"([^"]+)",\s*"value":\s*[^}]+}')
    
    matches = pattern.findall(ML_RESULT_REVIEW_PROMPT)
    
    assert len(matches) > 0, "No fix_config examples found in the prompt"

    json_policy_allowed_files = set(JSON_POLICY_PARAM_BOUNDS.keys())

    for path, key in matches:
        # Ignore placeholder examples like "<file.json>"
        if '<' in path or '>' in path or '<' in key or '>' in key:
            continue

        if path in ALPHA_CONFIG_ALLOWED_FILES:
            # This is for the alpha_config_patcher
            assert key == "label_profile", \
                f"Example for '{path}' has an invalid key '{key}'. Only 'label_profile' is allowed."
        elif path in json_policy_allowed_files:
            # This is for the json_policy_patcher. Use its internal validation function.
            bounds = get_json_policy_bounds(path, key)
            assert bounds is not None, \
                f"Example key '{key}' for path '{path}' is not valid according to the JSON policy patcher's bounds logic."
        else:
            pytest.fail(f"Example path '{path}' is not in any known patcher whitelist.")

