
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

@pytest.fixture
def mock_patchers(monkeypatch):
    """Mocks the update functions of both patcher modules."""
    mock_json_patcher = MagicMock(return_value={"ok": True, "backup": "test_backup"})
    mock_alpha_patcher = MagicMock(return_value={"ok": True, "backup": "test_backup"})

    # Since the function is async, the mock needs to be awaitable
    async_mock_json_patcher = AsyncMock(return_value={"ok": True, "backup": "test_backup"})
    async_mock_alpha_patcher = AsyncMock(return_value={"ok": True, "backup": "test_backup"})

    # We need to import the target module here to patch it
    from scripts.ai_trader.config import json_policy_patcher, alpha_config_patcher
    from scripts.ai_trader import control_event_consumer

    monkeypatch.setattr(json_policy_patcher, 'update', mock_json_patcher)
    monkeypatch.setattr(alpha_config_patcher, 'update', mock_alpha_patcher)
    
    # Also mock the .get method for the profit guard test
    mock_json_getter = MagicMock()
    monkeypatch.setattr(json_policy_patcher, 'get', mock_json_getter)

    # The module under test
    consumer_module = control_event_consumer
    
    return consumer_module, mock_json_patcher, mock_alpha_patcher, mock_json_getter


async def test_execute_fix_config_routes_alpha_config_paths_to_alpha_config_patcher(mock_patchers):
    """
    Tests that _execute_fix_config correctly routes calls to the
    alpha_config_patcher for alpha config files.
    """
    consumer, mock_json, mock_alpha, _ = mock_patchers

    # 1. Test path for alpha_config_patcher
    await consumer._execute_fix_config(
        params={
            "path": "alpha_config_enhanced.json",
            "key": "label_profile",
            "value": "new_profile"
        },
        job_id="test_job_1"
    )

    mock_alpha.assert_called_once_with(file_path="alpha_config_enhanced.json", key="label_profile", value="new_profile")
    mock_json.assert_not_called()

    # Reset mocks
    mock_alpha.reset_mock()
    mock_json.reset_mock()

    # 2. Test path for json_policy_patcher
    await consumer._execute_fix_config(
        params={
            "path": "ml_policy.json",
            "key": "label.horizon_sec",
            "value": 120
        },
        job_id="test_job_2"
    )

    mock_alpha.assert_not_called()
    mock_json.assert_called_once_with(file_path="ml_policy.json", key="label.horizon_sec", value=120)
