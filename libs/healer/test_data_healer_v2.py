import pytest
from unittest.mock import MagicMock, patch
from libs.healer.data_healer_v2 import DataHealerV2, ActionResult
from libs.health import Issue, Action, Diagnosis, SystemHealthSnapshot

@pytest.fixture
def data_healer():
    """Provides a DataHealerV2 instance for testing."""
    return DataHealerV2()

def test_heal_action(data_healer):
    """Tests the heal_action method."""
    action = Action(action_id="test_action", action_type="test_action", is_safe_auto_execute=True)
    with patch.object(data_healer, '_execute_shell_command', return_value=ActionResult(action_id="test_action", action_type="shell_command", success=True, duration_sec=0.1)) as mock_execute:
        result = data_healer.heal_action(action)
        assert result.success
        mock_execute.assert_called_once_with(action)

def test_heal_issue(data_healer):
    """Tests the heal_issue method."""
    issue = Issue(issue_id="test_issue", issue_type="test_issue", description="A test issue")
    with patch.object(data_healer, '_issue_to_action', return_value=Action(action_id="test_action", action_type="test_action", is_safe_auto_execute=True)) as mock_issue_to_action:
        with patch.object(data_healer, 'heal_action', return_value=ActionResult(action_id="test_action", action_type="test_action", success=True, duration_sec=0.1)) as mock_heal_action:
            result = data_healer.heal_issue(issue)
            assert result.success
            mock_issue_to_action.assert_called_once_with(issue)
            mock_heal_action.assert_called_once()

def test_heal_diagnosis(data_healer):
    """Tests the heal_diagnosis method."""
    diagnosis = Diagnosis(diagnosis_id="test_diagnosis", recommended_actions=[Action(action_id="test_action", action_type="test_action", is_safe_auto_execute=True)])
    with patch.object(data_healer, 'heal_action', return_value=ActionResult(action_id="test_action", action_type="test_action", success=True, duration_sec=0.1)) as mock_heal_action:
        results = data_healer.heal_diagnosis(diagnosis)
        assert len(results) == 1
        assert results[0].success
        mock_heal_action.assert_called_once()

def test_heal_snapshot(data_healer):
    """Tests the heal_snapshot method."""
    snapshot = SystemHealthSnapshot(overall_status="healthy", blocking_issues=[])
    results = data_healer.heal_snapshot(snapshot)
    assert len(results) == 0

    snapshot.overall_status = "blocked"
    snapshot.blocking_issues.append(Issue(issue_id="test_issue", issue_type="test_issue", description="A test issue"))
    with patch.object(data_healer, 'heal_issue', return_value=ActionResult(action_id="test_action", action_type="test_action", success=True, duration_sec=0.1)) as mock_heal_issue:
        results = data_healer.heal_snapshot(snapshot)
        assert len(results) > 0
        assert results[0].success
        mock_heal_issue.assert_called()
