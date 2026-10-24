#!/usr/bin/env python3
"""
Integration Tests for Meta v3.4: Self-Healing Event Alignment

Tests that all self-healing code paths emit events to ml.events for observability.

Tests:
1. ml.events table schema exists
2. DataHealerV2 emits events for all action types
3. AutoRemediator emits events for sync actions
4. Event data integrity (required fields populated)
5. Event querying and aggregation
"""
import sys
import pytest
from pathlib import Path
from datetime import datetime, timedelta, timezone
import time

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from libs.db.ch_pool import get_client
from libs.healer.data_healer_v2 import DataHealerV2, ActionResult
from libs.health import Action, make_action_id
from libs.ml.event_emitter import (
    emit_self_heal_event,
    emit_auto_remediation_event,
    emit_ml_job_event,
    emit_data_sync_event,
    emit_preflight_event
)


class TestMetaV34SelfHealingEvents:
    """Test suite for Meta v3.4: Self-Healing Event Alignment"""

    @pytest.fixture(scope="class")
    def ch_client(self):
        """Get ClickHouse client"""
        return get_client()

    @pytest.fixture(scope="class")
    def setup_ml_events_table(self, ch_client):
        """Ensure ml.events table exists"""
        # Ensure target database exists in environments that only initialize trading/lake/sfs.
        ch_client.command("CREATE DATABASE IF NOT EXISTS ml")

        # Read and execute schema
        schema_file = _PROJECT_ROOT / "schema" / "clickhouse" / "ml" / "ml_events.sql"
        if schema_file.exists():
            with schema_file.open() as f:
                schema_sql = f.read()
                # Execute each statement separately
                for statement in schema_sql.split(';'):
                    statement = statement.strip()
                    if statement and not statement.startswith('--') and not statement.startswith('COMMENT'):
                        try:
                            ch_client.command(statement)
                        except Exception as e:
                            # Ignore "already exists" errors
                            if "already exists" not in str(e).lower():
                                raise

        # Verify table exists
        result = ch_client.query("""
            SELECT count() as cnt
            FROM system.tables
            WHERE database = 'ml' AND name = 'events'
        """)
        assert result[0]["cnt"] == 1, "ml.events table should exist"

        yield ch_client

        # Cleanup: delete test events
        ch_client.command("""
            DELETE FROM ml.events
            WHERE action_id LIKE 'test_%'
        """)

    def test_ml_events_schema(self, setup_ml_events_table):
        """Test 1: Verify ml.events table schema"""
        result = setup_ml_events_table.query("""
            SELECT name, type
            FROM system.columns
            WHERE database = 'ml' AND table = 'events'
            ORDER BY name
        """)

        columns = {row["name"]: row["type"] for row in result}

        # Core columns
        assert "ts" in columns
        assert "event" in columns
        assert "component" in columns
        assert "action_id" in columns
        assert "symbol" in columns
        assert "status" in columns

        # Self-healing specific
        assert "action_type" in columns
        assert "is_auto_executed" in columns
        assert "duration_sec" in columns
        assert "rows_affected" in columns

        # ML job specific
        assert "experiment_id" in columns
        assert "model_name" in columns
        assert "sharpe" in columns

        # Data quality specific
        assert "null_ratio" in columns
        assert "coverage_ratio" in columns

        # Metadata
        assert "metadata" in columns

    def test_emit_self_heal_event_basic(self, setup_ml_events_table):
        """Test 2: Basic self-heal event emission"""
        action_id = "test_heal_basic_001"

        # Emit event
        success = emit_self_heal_event(
            action_type="generate_label",
            action_id=action_id,
            status="success",
            symbol="TSLA",
            duration_sec=12.5,
            rows_affected=1200,
            reason="Label coverage low",
            metadata={"horizons": [30, 60]}
        )

        assert success, "Event emission should succeed"

        # Wait for ClickHouse to flush
        time.sleep(0.5)

        # Query event
        result = setup_ml_events_table.query("""
            SELECT *
            FROM ml.events
            WHERE action_id = %(action_id)s
        """, {"action_id": action_id})

        assert len(result) == 1, "Should find exactly 1 event"

        event = result[0]
        assert event["event"] == "self_heal.action_complete"
        assert event["component"] == "data_healer_v2"
        assert event["action_type"] == "generate_label"
        assert event["symbol"] == "TSLA"
        assert event["status"] == "success"
        assert event["duration_sec"] == 12.5
        assert event["rows_affected"] == 1200
        assert event["is_auto_executed"] == 1

    def test_data_healer_v2_emits_events(self, setup_ml_events_table):
        """Test 3: DataHealerV2 emits events for actions"""
        healer = DataHealerV2(dry_run=True)

        action_id = "test_heal_action_002"
        action = Action(
            action_id=action_id,
            action_type="rebuild_features",
            priority=1,
            title="Rebuild features for test",
            description="Test event emission",
            params={"symbols": ["AAPL"], "lookback_days": 7},
            is_safe_auto_execute=True,
            requires_approval=False
        )

        # Execute action (dry run, so won't actually run)
        result = healer.heal_action(action)

        assert result.success, "Dry run should succeed"

        # Wait for event flush
        time.sleep(0.5)

        # Query events - should have start event (dry run doesn't complete)
        events = setup_ml_events_table.query("""
            SELECT event, action_type, symbol
            FROM ml.events
            WHERE action_id = %(action_id)s
            ORDER BY ts
        """, {"action_id": action_id})

        # In dry run mode, we emit start + success with metadata.dry_run=True
        assert len(events) >= 1, "Should have at least start event"
        assert events[0]["event"] in ["self_heal.action_start", "self_heal.action_complete"]
        assert events[0]["action_type"] == "rebuild_features"
        assert events[0]["symbol"] == "AAPL"

    def test_auto_remediation_event_emission(self, setup_ml_events_table):
        """Test 4: AutoRemediator emits events"""
        action_id = "test_remediation_003"

        success = emit_auto_remediation_event(
            action_taken="sync_features_active",
            status="success",
            symbol="TSLA",
            duration_sec=45.2,
            reason="Features stale, synced from lake",
            metadata={"rows_synced": 5000, "recheck_status": "ready"}
        )

        assert success, "Remediation event should emit successfully"

        time.sleep(0.5)

        # Query event
        result = setup_ml_events_table.query("""
            SELECT event, component, action_type, status, duration_sec
            FROM ml.events
            WHERE event = 'auto_remediation.sync_features_active'
            ORDER BY ts DESC
            LIMIT 1
        """)

        assert len(result) == 1
        event = result[0]
        assert event["component"] == "auto_remediator"
        assert event["action_type"] == "sync_features_active"
        assert event["status"] == "success"
        assert event["duration_sec"] == 45.2

    def test_ml_job_event_emission(self, setup_ml_events_table):
        """Test 5: ML job events"""
        experiment_id = "test_exp_004"

        success = emit_ml_job_event(
            event="ml.job.complete",
            experiment_id=experiment_id,
            model_name="lightgbm",
            status="success",
            sharpe=1.85,
            duration_sec=120.5,
            symbol="TSLA",
            metadata={"train_samples": 50000, "val_samples": 10000}
        )

        assert success

        time.sleep(0.5)

        result = setup_ml_events_table.query("""
            SELECT event, component, experiment_id, model_name, sharpe
            FROM ml.events
            WHERE experiment_id = %(experiment_id)s
            ORDER BY ts DESC
            LIMIT 1
        """, {"experiment_id": experiment_id})

        assert len(result) == 1
        event = result[0]
        assert event["event"] == "ml.job.complete"
        assert event["component"] == "ml_scheduler"
        assert event["model_name"] == "lightgbm"
        assert event["sharpe"] == 1.85

    def test_data_sync_event_emission(self, setup_ml_events_table):
        """Test 6: Data sync events"""
        action_id = "test_sync_005"

        success = emit_data_sync_event(
            status="success",
            symbol="AAPL",
            rows_synced=3500,
            duration_sec=22.3,
            reason="Active table sync completed",
            metadata={"source_table": "lake.features_1m", "target_table": "trading.features_1m_active"}
        )

        assert success

        time.sleep(0.5)

        result = setup_ml_events_table.query("""
            SELECT event, component, rows_affected, status
            FROM ml.events
            WHERE event = 'active_table.sync.complete'
            ORDER BY ts DESC
            LIMIT 1
        """)

        assert len(result) == 1
        event = result[0]
        assert event["component"] == "active_table_manager"
        assert event["rows_affected"] == 3500
        assert event["status"] == "success"

    def test_preflight_event_emission(self, setup_ml_events_table):
        """Test 7: Preflight check events"""
        action_id = "test_preflight_006"

        success = emit_preflight_event(
            status="failed",
            symbol="TSLA",
            reason="Label coverage below 80%",
            null_ratio=0.15,
            coverage_ratio=0.72,
            metadata={"min_rows": 1000, "actual_rows": 720}
        )

        assert success

        time.sleep(0.5)

        result = setup_ml_events_table.query("""
            SELECT event, status, null_ratio, coverage_ratio
            FROM ml.events
            WHERE event = 'preflight.check_failed'
            ORDER BY ts DESC
            LIMIT 1
        """)

        assert len(result) == 1
        event = result[0]
        assert event["status"] == "failed"
        assert event["null_ratio"] == 0.15
        assert event["coverage_ratio"] == 0.72

    def test_event_aggregation_queries(self, setup_ml_events_table):
        """Test 8: Event aggregation for observability"""
        # Insert multiple test events
        for i in range(5):
            emit_self_heal_event(
                action_type="generate_label",
                action_id=f"test_agg_{i:03d}",
                status="success" if i % 2 == 0 else "failed",
                symbol="TSLA",
                duration_sec=10.0 + i,
                rows_affected=1000 + i * 100
            )

        time.sleep(0.5)

        # Aggregation query: Count by status
        result = setup_ml_events_table.query("""
            SELECT
                status,
                count() as total,
                avg(duration_sec) as avg_duration,
                sum(rows_affected) as total_rows
            FROM ml.events
            WHERE action_id LIKE 'test_agg_%'
            GROUP BY status
            ORDER BY status
        """)

        status_map = {row["status"]: row for row in result}

        assert "success" in status_map
        assert "failed" in status_map
        assert status_map["success"]["total"] == 3  # 0, 2, 4
        assert status_map["failed"]["total"] == 2   # 1, 3

    def test_event_time_range_queries(self, setup_ml_events_table):
        """Test 9: Time-based event queries"""
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        # Query recent events
        result = setup_ml_events_table.query("""
            SELECT count() as cnt
            FROM ml.events
            WHERE ts >= %(start_time)s
              AND action_id LIKE 'test_%%'
        """, {"start_time": one_hour_ago})

        assert result[0]["cnt"] > 0, "Should have recent test events"

    def test_event_metadata_json(self, setup_ml_events_table):
        """Test 10: JSON metadata handling"""
        action_id = "test_metadata_010"

        emit_self_heal_event(
            action_type="backfill_bars",
            action_id=action_id,
            status="success",
            symbol="AAPL",
            metadata={
                "bar_size": "1m",
                "days_backfilled": 3,
                "symbols": ["AAPL", "TSLA"],
                "performance": {
                    "bars_per_sec": 1250.5,
                    "total_bars": 12000
                }
            }
        )

        time.sleep(0.5)

        result = setup_ml_events_table.query("""
            SELECT metadata
            FROM ml.events
            WHERE action_id = %(action_id)s
        """, {"action_id": action_id})

        assert len(result) == 1

        # ClickHouse returns JSON as string
        import json
        metadata = json.loads(result[0]["metadata"])

        assert metadata["bar_size"] == "1m"
        assert metadata["days_backfilled"] == 3
        assert len(metadata["symbols"]) == 2
        assert "performance" in metadata
        assert metadata["performance"]["bars_per_sec"] == 1250.5


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
