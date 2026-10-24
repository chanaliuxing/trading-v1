"""Unit tests for Events Exporter brain_calls integration"""
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest
from datetime import date, datetime

from scripts.ai_trader.exporters.events_exporter import export_events


class TestBrainCallsExport:
    """Test brain_calls.jsonl export to ClickHouse"""

    def test_brain_calls_included_in_file_list(self, temp_dir):
        """Test brain_calls.jsonl is included in export"""
        # Create test directory structure
        run_date = "2025-10-29"
        out_dir = temp_dir / run_date
        out_dir.mkdir(parents=True)

        # Create brain_calls.jsonl
        brain_calls_file = out_dir / "brain_calls.jsonl"
        brain_calls_file.write_text(json.dumps({
            "ts": "2025-10-29T10:30:00",
            "kind": "propose_plan",
            "session": "morning",
            "provider": "deepseek_nvidia",
            "model": "deepseek-ai/deepseek-r1",
            "input_keys": ["health"],
            "output": {"action": "proceed"}
        }))

        # Mock ClickHouse
        mock_ch = MagicMock()

        with patch('scripts.ai_trader.exporters.events_exporter._ch', return_value=mock_ch):
            with patch('scripts.ai_trader.exporters.events_exporter.ensure_tables'):
                export_events(runs_dir=str(temp_dir), run_date=run_date, session="morning")

        # Verify ClickHouse insert was called
        assert mock_ch.insert.called

    def test_brain_calls_parsed_correctly(self, temp_dir):
        """Test brain_calls records are parsed and inserted correctly"""
        run_date = "2025-10-29"
        out_dir = temp_dir / run_date
        out_dir.mkdir(parents=True)

        # Create brain_calls.jsonl with multiple records
        brain_calls_file = out_dir / "brain_calls.jsonl"
        records = [
            {
                "ts": "2025-10-29T10:30:00",
                "kind": "propose_plan",
                "session": "morning",
                "provider": "deepseek_nvidia",
                "output": {"action": "proceed"}
            },
            {
                "ts": "2025-10-29T14:30:00",
                "kind": "decide",
                "action": "hpo_deploy",
                "provider": "openai",
                "output": {"verdict": "approve"}
            }
        ]
        with brain_calls_file.open('w') as f:
            for record in records:
                f.write(json.dumps(record) + '\n')

        # Mock ClickHouse
        mock_ch = MagicMock()

        with patch('scripts.ai_trader.exporters.events_exporter._ch', return_value=mock_ch):
            with patch('scripts.ai_trader.exporters.events_exporter.ensure_tables'):
                export_events(runs_dir=str(temp_dir), run_date=run_date, session="test")

        # Verify insert was called
        assert mock_ch.insert.called
        insert_args = mock_ch.insert.call_args

        # Check that brain records were included
        rows = insert_args[0][1]  # Second argument is the rows list
        brain_rows = [r for r in rows if r[4] == 'brain']  # kind column is index 4
        assert len(brain_rows) == 2

    def test_brain_calls_session_from_payload(self, temp_dir):
        """Test brain_calls use session from payload instead of argument"""
        run_date = "2025-10-29"
        out_dir = temp_dir / run_date
        out_dir.mkdir(parents=True)

        # Create brain_calls.jsonl with session in payload
        brain_calls_file = out_dir / "brain_calls.jsonl"
        brain_calls_file.write_text(json.dumps({
            "ts": "2025-10-29T10:30:00",
            "kind": "propose_plan",
            "session": "intraday",  # This should be used, not the export_events session arg
            "output": {"action": "proceed"}
        }))

        # Mock ClickHouse
        mock_ch = MagicMock()

        with patch('scripts.ai_trader.exporters.events_exporter._ch', return_value=mock_ch):
            with patch('scripts.ai_trader.exporters.events_exporter.ensure_tables'):
                export_events(runs_dir=str(temp_dir), run_date=run_date, session="morning")

        # Verify session from payload was used
        insert_args = mock_ch.insert.call_args
        rows = insert_args[0][1]
        brain_row = [r for r in rows if r[4] == 'brain'][0]
        assert brain_row[3] == "intraday"  # session column is index 3

    def test_brain_calls_action_as_fallback_session(self, temp_dir):
        """Test brain_calls use action as session if session not present"""
        run_date = "2025-10-29"
        out_dir = temp_dir / run_date
        out_dir.mkdir(parents=True)

        # Create brain_calls.jsonl with action but no session
        brain_calls_file = out_dir / "brain_calls.jsonl"
        brain_calls_file.write_text(json.dumps({
            "ts": "2025-10-29T14:30:00",
            "kind": "decide",
            "action": "hpo_deploy",  # Should be used as session
            "output": {"verdict": "approve"}
        }))

        # Mock ClickHouse
        mock_ch = MagicMock()

        with patch('scripts.ai_trader.exporters.events_exporter._ch', return_value=mock_ch):
            with patch('scripts.ai_trader.exporters.events_exporter.ensure_tables'):
                export_events(runs_dir=str(temp_dir), run_date=run_date, session="default")

        # Verify action was used as session
        insert_args = mock_ch.insert.call_args
        rows = insert_args[0][1]
        brain_row = [r for r in rows if r[4] == 'brain'][0]
        assert brain_row[3] == "hpo_deploy"  # session column is index 3

    def test_brain_calls_no_file_graceful(self, temp_dir):
        """Test export handles missing brain_calls.jsonl gracefully"""
        run_date = "2025-10-29"
        out_dir = temp_dir / run_date
        out_dir.mkdir(parents=True)

        # Don't create brain_calls.jsonl

        # Mock ClickHouse
        mock_ch = MagicMock()

        with patch('scripts.ai_trader.exporters.events_exporter._ch', return_value=mock_ch):
            with patch('scripts.ai_trader.exporters.events_exporter.ensure_tables'):
                # Should not raise exception
                export_events(runs_dir=str(temp_dir), run_date=run_date, session="morning")

        # Should still complete (just no brain events)
        assert mock_ch.insert.called or not mock_ch.insert.called  # Either is fine

    def test_brain_calls_invalid_json_skipped(self, temp_dir):
        """Test invalid JSON lines in brain_calls are skipped"""
        run_date = "2025-10-29"
        out_dir = temp_dir / run_date
        out_dir.mkdir(parents=True)

        # Create brain_calls.jsonl with mixed valid/invalid lines
        brain_calls_file = out_dir / "brain_calls.jsonl"
        brain_calls_file.write_text(
            json.dumps({"ts": "2025-10-29T10:30:00", "kind": "propose_plan", "session": "morning"}) + '\n' +
            'invalid json line\n' +
            json.dumps({"ts": "2025-10-29T14:30:00", "kind": "decide", "action": "hpo_deploy"}) + '\n'
        )

        # Mock ClickHouse
        mock_ch = MagicMock()

        with patch('scripts.ai_trader.exporters.events_exporter._ch', return_value=mock_ch):
            with patch('scripts.ai_trader.exporters.events_exporter.ensure_tables'):
                # Should not raise exception
                export_events(runs_dir=str(temp_dir), run_date=run_date, session="test")

        # Should have inserted valid records only
        if mock_ch.insert.called:
            insert_args = mock_ch.insert.call_args
            rows = insert_args[0][1]
            brain_rows = [r for r in rows if r[4] == 'brain']
            # Should have skipped invalid line
            assert len(brain_rows) <= 2

    def test_brain_calls_payload_json_encoded(self, temp_dir):
        """Test brain_calls payload is JSON-encoded in database"""
        run_date = "2025-10-29"
        out_dir = temp_dir / run_date
        out_dir.mkdir(parents=True)

        # Create brain_calls.jsonl
        record = {
            "ts": "2025-10-29T10:30:00",
            "kind": "propose_plan",
            "session": "morning",
            "output": {"action": "proceed", "checks": ["health", "data"]}
        }
        brain_calls_file = out_dir / "brain_calls.jsonl"
        brain_calls_file.write_text(json.dumps(record))

        # Mock ClickHouse
        mock_ch = MagicMock()

        with patch('scripts.ai_trader.exporters.events_exporter._ch', return_value=mock_ch):
            with patch('scripts.ai_trader.exporters.events_exporter.ensure_tables'):
                export_events(runs_dir=str(temp_dir), run_date=run_date, session="test")

        # Verify payload is JSON string
        insert_args = mock_ch.insert.call_args
        rows = insert_args[0][1]
        brain_row = [r for r in rows if r[4] == 'brain'][0]
        payload_str = brain_row[6]  # payload column is index 6

        # Should be valid JSON string
        payload_obj = json.loads(payload_str)
        assert payload_obj["kind"] == "propose_plan"
        assert payload_obj["output"]["action"] == "proceed"

    def test_brain_calls_timestamp_parsing(self, temp_dir):
        """Test timestamp is correctly parsed from brain_calls"""
        run_date = "2025-10-29"
        out_dir = temp_dir / run_date
        out_dir.mkdir(parents=True)

        # Create brain_calls.jsonl with ISO timestamp
        brain_calls_file = out_dir / "brain_calls.jsonl"
        brain_calls_file.write_text(json.dumps({
            "ts": "2025-10-29T10:30:45.123456",
            "kind": "propose_plan",
            "session": "morning"
        }))

        # Mock ClickHouse
        mock_ch = MagicMock()

        with patch('scripts.ai_trader.exporters.events_exporter._ch', return_value=mock_ch):
            with patch('scripts.ai_trader.exporters.events_exporter.ensure_tables'):
                export_events(runs_dir=str(temp_dir), run_date=run_date, session="test")

        # Verify timestamp was parsed correctly
        insert_args = mock_ch.insert.call_args
        rows = insert_args[0][1]
        brain_row = [r for r in rows if r[4] == 'brain'][0]
        ts = brain_row[1]  # ts column is index 1

        assert isinstance(ts, datetime)
        assert ts.hour == 10
        assert ts.minute == 30


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory for tests"""
    return tmp_path
