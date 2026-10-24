#!/usr/bin/env python3
"""
Integration tests for P1.1: ResearchPolicy/State Deep Integration

Verifies:
1. collect_research_snapshot_enhanced uses real ClickHouse queries
2. Strategy metrics from trading.oof_xgb table
3. Compute budget from ml.events or file fallback
4. Immune cases loaded from runs/memory
5. Mutable targets loaded from snapshot
"""
from __future__ import annotations

import pytest
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta


class TestP11ResearchState:
    """Test P1.1: ResearchPolicy/State深度集成"""

    def test_state_enhanced_import(self):
        """Verify enhanced state module can be imported"""
        from scripts.ai_trader.research.state_enhanced import (
            collect_research_snapshot_enhanced,
            collect_research_snapshot,
        )

        assert collect_research_snapshot_enhanced is not None
        assert collect_research_snapshot is not None

    @patch("scripts.ai_trader.research.state_enhanced.get_client")
    def test_oof_performance_query(self, mock_get_client):
        """Test OOF performance query from ClickHouse"""
        from scripts.ai_trader.research.state_enhanced import _query_recent_oof_performance

        # Mock ClickHouse client
        mock_ch = Mock()
        mock_get_client.return_value = mock_ch

        # Mock query result
        mock_ch.query.return_value = [
            {
                "symbol": "TSLA",
                "horizon_min": 30,
                "model_type": "xgb",
                "auc": 0.62,
                "ic": 0.05,
                "n": 1200,
                "last_ts": datetime(2025, 12, 3, 16, 0),
            },
            {
                "symbol": "NVDA",
                "horizon_min": 30,
                "model_type": "xgb",
                "auc": 0.58,
                "ic": 0.03,
                "n": 980,
                "last_ts": datetime(2025, 12, 3, 16, 0),
            },
        ]

        results = _query_recent_oof_performance(mock_ch, days=30)

        # Verify results structure
        assert "intraday_TSLA_h30" in results
        assert "intraday_NVDA_h30" in results

        tsla_metrics = results["intraday_TSLA_h30"]
        assert tsla_metrics["auc"] == 0.62
        assert tsla_metrics["ic"] == 0.05
        assert tsla_metrics["n"] == 1200
        assert tsla_metrics["model_type"] == "xgb"

        # Verify query was called
        mock_ch.query.assert_called_once()
        # Verify query contains expected table
        call_args = mock_ch.query.call_args
        query_str = call_args[0][0] if call_args[0] else ""
        assert "trading.oof_xgb" in query_str

    @patch("scripts.ai_trader.research.state_enhanced.get_client")
    def test_compute_budget_query(self, mock_get_client):
        """Test compute budget usage query"""
        from scripts.ai_trader.research.state_enhanced import _query_compute_budget_usage

        mock_ch = Mock()
        mock_get_client.return_value = mock_ch

        # Mock ml.events query result
        mock_ch.query.return_value = [
            {"hours_used": 8.5}
        ]

        total, used = _query_compute_budget_usage(mock_ch, days=7)

        assert total == 24.0 * 7  # 7 days * 24 hours
        assert used == 8.5

    def test_immune_cases_loading(self, tmp_path):
        """Test immune cases are loaded from file"""
        from scripts.ai_trader.research.state_enhanced import _load_immune_cases

        # Create temp immune cases file
        immune_file = tmp_path / "immune_cases.jsonl"
        immune_data = [
            {"error": "ClickHouse timeout", "module": "data_fetcher", "count": 3},
            {"error": "Label null ratio high", "symbol": "TSLA", "count": 1},
        ]

        with immune_file.open("w") as f:
            for case in immune_data:
                f.write(json.dumps(case) + "\n")

        # Patch Path to use temp directory
        with patch("scripts.ai_trader.research.state_enhanced.Path") as mock_path:
            mock_path.return_value = immune_file
            cases = _load_immune_cases()

            # Verify loading
            assert len(cases) == 2
            assert cases[0]["error"] == "ClickHouse timeout"
            assert cases[1]["symbol"] == "TSLA"

    def test_mutable_targets_loading(self, tmp_path):
        """Test mutable targets snapshot loading"""
        from scripts.ai_trader.research.state_enhanced import _load_mutable_targets

        # Create temp mutable targets file
        mt_file = tmp_path / "mutable_targets.json"
        mt_data = {
            "data:features_1m_active": {
                "metrics": {
                    "health_score": 0.85,
                    "recommendation": "healthy",
                }
            },
            "module:feature_dsl": {
                "metrics": {
                    "health_score": 0.6,
                    "recommendation": "monitor",
                }
            },
        }

        mt_file.write_text(json.dumps(mt_data))

        with patch("scripts.ai_trader.research.state_enhanced.Path") as mock_path:
            mock_path.return_value = mt_file
            targets = _load_mutable_targets()

            assert "data:features_1m_active" in targets
            assert "module:feature_dsl" in targets
            assert targets["data:features_1m_active"]["metrics"]["health_score"] == 0.85

    @patch("scripts.ai_trader.research.state_enhanced.get_client")
    def test_collect_snapshot_enhanced_with_ch(self, mock_get_client):
        """Test full snapshot collection with ClickHouse"""
        from scripts.ai_trader.research.state_enhanced import collect_research_snapshot_enhanced

        # Setup mock ClickHouse
        mock_ch = Mock()
        mock_get_client.return_value = mock_ch

        # Mock OOF query
        mock_ch.query.side_effect = [
            # First call: OOF performance
            [
                {
                    "symbol": "TSLA",
                    "horizon_min": 30,
                    "model_type": "xgb",
                    "auc": 0.62,
                    "ic": 0.05,
                    "n": 1200,
                    "last_ts": datetime(2025, 12, 3),
                }
            ],
            # Second call: compute budget
            [{"hours_used": 8.5}],
        ]

        snapshot = collect_research_snapshot_enhanced(policy_version="3.0.0")

        # Verify snapshot structure
        assert snapshot.policy_version == "3.0.0"
        assert snapshot.timestamp is not None

        # Verify strategies loaded
        assert len(snapshot.strategies) > 0
        if "intraday_TSLA_h30" in snapshot.strategies:
            tsla_strat = snapshot.strategies["intraday_TSLA_h30"]
            assert tsla_strat.metrics.oos["auc"] == 0.62
            assert tsla_strat.domain == "trading"

        # Verify budgets
        assert snapshot.budgets.compute_hours.total > 0
        assert snapshot.budgets.compute_hours.used >= 0

    def test_collect_snapshot_fallback_without_ch(self):
        """Test snapshot collection without ClickHouse (file fallback)"""
        from scripts.ai_trader.research.state_enhanced import collect_research_snapshot_enhanced

        # Force get_client to None
        with patch("scripts.ai_trader.research.state_enhanced.get_client", None):
            snapshot = collect_research_snapshot_enhanced()

            # Should still return valid snapshot with placeholders
            assert snapshot is not None
            assert snapshot.strategies is not None
            assert snapshot.budgets is not None
            assert "intraday_placeholder" in snapshot.strategies

    def test_backwards_compatibility(self):
        """Test that collect_research_snapshot wrapper works"""
        from scripts.ai_trader.research.state_enhanced import collect_research_snapshot

        with patch("scripts.ai_trader.research.state_enhanced.get_client", None):
            snapshot = collect_research_snapshot(policy_version="3.0.0")

            assert snapshot.policy_version == "3.0.0"
            assert isinstance(snapshot.strategies, dict)

    def test_snapshot_to_json(self):
        """Test snapshot can be serialized to JSON"""
        from scripts.ai_trader.research.state_enhanced import collect_research_snapshot

        with patch("scripts.ai_trader.research.state_enhanced.get_client", None):
            snapshot = collect_research_snapshot()
            json_dict = snapshot.to_json_dict()

            # Verify JSON structure
            assert "timestamp" in json_dict
            assert "policy_version" in json_dict
            assert "strategies" in json_dict
            assert "budgets" in json_dict
            assert "immune_cases" in json_dict

            # Should be JSON serializable
            json_str = json.dumps(json_dict, default=str)
            assert len(json_str) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
