#!/usr/bin/env python3
"""
Integration tests for P0.2: Active Table Sync Automation

Verifies:
1. ActiveTableManager can detect sync_or_fallback health status
2. Sync is triggered when needed
3. Fallback to baseline on sync failure
4. Events are recorded properly
"""
from __future__ import annotations

import pytest
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any


class TestP02ActiveSync:
    """Test P0.2: Active同步自动化"""

    @pytest.fixture
    def temp_snapshot(self, tmp_path):
        """Create temporary mutable_targets snapshot"""
        snapshot_path = tmp_path / "mutable_targets.json"
        yield snapshot_path

    @pytest.fixture
    def snapshot_sync_needed(self, temp_snapshot):
        """Snapshot indicating sync is needed"""
        data = {
            "data:features_1m_active": {
                "metrics": {
                    "recommendation": "sync_or_fallback",
                    "health_score": 0.3,
                }
            }
        }
        temp_snapshot.write_text(json.dumps(data))
        return temp_snapshot

    @pytest.fixture
    def snapshot_healthy(self, temp_snapshot):
        """Snapshot indicating table is healthy"""
        data = {
            "data:features_1m_active": {
                "metrics": {
                    "recommendation": "healthy",
                    "health_score": 0.9,
                }
            }
        }
        temp_snapshot.write_text(json.dumps(data))
        return temp_snapshot

    def test_active_table_manager_import(self):
        """Verify ActiveTableManager can be imported"""
        from libs.ml.active_table_manager import ActiveTableManager, SyncResult

        assert ActiveTableManager is not None
        assert SyncResult is not None

    def test_check_should_sync_when_healthy(self, snapshot_healthy):
        """Test that sync is NOT triggered when table is healthy"""
        from libs.ml.active_table_manager import ActiveTableManager

        manager = ActiveTableManager(snapshot_path=str(snapshot_healthy))
        should_sync = manager.check_should_sync(["TSLA"])

        assert should_sync is False, "Should not sync when table is healthy"

    def test_check_should_sync_when_needed(self, snapshot_sync_needed):
        """Test that sync IS triggered when recommendation is sync_or_fallback"""
        from libs.ml.active_table_manager import ActiveTableManager

        manager = ActiveTableManager(snapshot_path=str(snapshot_sync_needed))
        should_sync = manager.check_should_sync(["TSLA"])

        assert should_sync is True, "Should sync when recommendation is sync_or_fallback"

    def test_fallback_to_baseline(self):
        """Test fallback creates correct SyncResult"""
        from libs.ml.active_table_manager import ActiveTableManager

        manager = ActiveTableManager()
        result = manager.fallback_to_baseline()

        assert result.success is True
        assert result.action == "fallback"
        assert result.features_table == "trading.features_1m"
        assert result.event is not None
        assert "fallback_to_baseline" in result.event["event"]

    @patch("subprocess.run")
    def test_sync_success(self, mock_run, temp_snapshot):
        """Test successful sync execution"""
        from libs.ml.active_table_manager import ActiveTableManager

        # Mock successful subprocess
        mock_run.return_value = Mock(
            returncode=0,
            stdout="synced_rows=1234\n",
            stderr="",
        )

        manager = ActiveTableManager(snapshot_path=str(temp_snapshot))
        result = manager.sync_active_from_baseline(
            symbols=["TSLA", "NVDA"],
            start=datetime(2025, 11, 1),
            end=datetime(2025, 12, 1),
        )

        assert result.success is True
        assert result.action == "sync"
        assert result.rows_synced == 1234
        assert result.features_table == "trading.features_1m_active"
        assert result.event is not None
        assert result.event["event"] == "active_table.sync_completed"
        assert result.event["rows_synced"] == 1234

        # Verify subprocess was called correctly
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "sync_features_active.py" in " ".join(call_args)
        assert "--symbols" in call_args
        assert "TSLA,NVDA" in call_args

    @patch("subprocess.run")
    def test_sync_failure(self, mock_run, temp_snapshot):
        """Test sync failure handling"""
        from libs.ml.active_table_manager import ActiveTableManager

        # Mock failed subprocess
        mock_run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="ClickHouse connection error",
        )

        manager = ActiveTableManager(snapshot_path=str(temp_snapshot))
        result = manager.sync_active_from_baseline(
            symbols=["TSLA"],
            start=datetime(2025, 11, 1),
            end=datetime(2025, 12, 1),
        )

        assert result.success is False
        assert result.action == "sync"
        assert "ClickHouse connection error" in result.error
        assert result.event is not None
        assert "sync_failed" in result.event["event"]

    @patch("subprocess.run")
    def test_auto_sync_or_fallback_when_sync_fails(self, mock_run, snapshot_sync_needed):
        """Test that fallback happens when sync fails"""
        from libs.ml.active_table_manager import ActiveTableManager

        # Mock failed sync
        mock_run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="sync error",
        )

        manager = ActiveTableManager(snapshot_path=str(snapshot_sync_needed))
        result = manager.auto_sync_or_fallback(["TSLA"])

        # Should fallback to baseline
        assert result.success is True
        assert result.action == "fallback"
        assert result.features_table == "trading.features_1m"
        assert "sync_error" in result.event

    @patch("subprocess.run")
    def test_auto_sync_or_fallback_when_sync_succeeds(self, mock_run, snapshot_sync_needed):
        """Test that active table is used when sync succeeds"""
        from libs.ml.active_table_manager import ActiveTableManager

        # Mock successful sync
        mock_run.return_value = Mock(
            returncode=0,
            stdout="synced_rows=5000\n",
            stderr="",
        )

        manager = ActiveTableManager(snapshot_path=str(snapshot_sync_needed))
        result = manager.auto_sync_or_fallback(["TSLA"])

        # Should use active table
        assert result.success is True
        assert result.action == "sync"
        assert result.features_table == "trading.features_1m_active"
        assert result.rows_synced == 5000

    def test_auto_sync_or_fallback_no_action_when_healthy(self, snapshot_healthy):
        """Test that no action is taken when table is healthy"""
        from libs.ml.active_table_manager import ActiveTableManager

        manager = ActiveTableManager(snapshot_path=str(snapshot_healthy))
        result = manager.auto_sync_or_fallback(["TSLA"])

        assert result.success is True
        assert result.action == "no_action"
        assert result.features_table == "trading.features_1m_active"

    def test_create_event_structure(self):
        """Test event structure is correct"""
        from libs.ml.active_table_manager import ActiveTableManager

        manager = ActiveTableManager()
        event = manager._create_event(
            event_type="test_event",
            custom_field="custom_value",
        )

        assert "event" in event
        assert event["event"] == "active_table.test_event"
        assert "timestamp" in event
        assert "active_table" in event
        assert "baseline_table" in event
        assert event["custom_field"] == "custom_value"

    @pytest.mark.parametrize("recommendation,expected", [
        ("sync_or_fallback", True),
        ("healthy", False),
        ("monitor", False),
        (None, False),
    ])
    def test_check_should_sync_various_recommendations(self, recommendation, expected, temp_snapshot):
        """Test sync decision for various health recommendations"""
        from libs.ml.active_table_manager import ActiveTableManager

        data = {
            "data:features_1m_active": {
                "metrics": {
                    "recommendation": recommendation,
                }
            }
        }
        temp_snapshot.write_text(json.dumps(data))

        manager = ActiveTableManager(snapshot_path=str(temp_snapshot))
        should_sync = manager.check_should_sync(["TSLA"])

        assert should_sync == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
