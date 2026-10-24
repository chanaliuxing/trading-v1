"""Unit tests for Memory modules (episode_store and aggregator)"""
import json
from pathlib import Path
from datetime import date
from unittest.mock import Mock, patch, MagicMock, AsyncMock
import pytest

from scripts.ai_trader.memory import episode_store, aggregator


class TestEpisodeStoreHelpers:
    """Test episode_store helper functions"""

    def test_stable_hash_consistent(self):
        """Test hash is consistent for same input"""
        obj1 = {"a": 1, "b": 2}
        obj2 = {"b": 2, "a": 1}  # Different order

        hash1 = episode_store._stable_hash(obj1)
        hash2 = episode_store._stable_hash(obj2)

        assert hash1 == hash2  # Same content, same hash

    def test_stable_hash_different(self):
        """Test hash is different for different input"""
        obj1 = {"a": 1}
        obj2 = {"a": 2}

        hash1 = episode_store._stable_hash(obj1)
        hash2 = episode_store._stable_hash(obj2)

        assert hash1 != hash2

    def test_read_reflection_exists(self, temp_dir):
        """Test reading existing reflection file"""
        runs_dir = temp_dir / "runs"
        runs_dir.mkdir()
        ref_dir = runs_dir / "2025-10-29"
        ref_dir.mkdir()

        reflection = {
            "aggregate": {"overall_consistency": 0.85},
            "details": [{"symbol": "AAPL", "provider": "openai"}]
        }
        ref_file = ref_dir / "reflection.json"
        ref_file.write_text(json.dumps(reflection))

        result = episode_store._read_reflection(str(runs_dir), "2025-10-29")

        assert result["aggregate"]["overall_consistency"] == 0.85

    def test_read_reflection_missing(self, temp_dir):
        """Test reading missing reflection file returns empty dict"""
        result = episode_store._read_reflection(str(temp_dir), "2025-10-29")

        assert result == {}

    def test_read_reflection_invalid_json(self, temp_dir):
        """Test reading invalid JSON returns empty dict"""
        runs_dir = temp_dir / "runs"
        runs_dir.mkdir()
        ref_dir = runs_dir / "2025-10-29"
        ref_dir.mkdir()

        ref_file = ref_dir / "reflection.json"
        ref_file.write_text("{ invalid json }")

        result = episode_store._read_reflection(str(runs_dir), "2025-10-29")

        assert result == {}

    def test_symbols_from_reflection(self):
        """Test extracting symbols from reflection"""
        reflection = {
            "details": [
                {"symbol": "AAPL", "provider": "openai"},
                {"symbol": "TSLA", "provider": "gemini"},
                {"symbol": "AAPL", "provider": "deepseek"}  # Duplicate
            ]
        }

        result = episode_store._symbols_from_reflection(reflection)

        assert result == ["AAPL", "TSLA"]  # Sorted, unique

    def test_symbols_from_reflection_empty(self):
        """Test extracting symbols from empty reflection"""
        result = episode_store._symbols_from_reflection({})

        assert result == []

    def test_providers_from_reflection(self):
        """Test extracting providers from reflection"""
        reflection = {
            "details": [
                {"symbol": "AAPL", "provider": "openai"},
                {"symbol": "TSLA", "provider": "gemini"},
                {"symbol": "NVDA", "provider": "openai"}  # Duplicate provider
            ]
        }

        result = episode_store._providers_from_reflection(reflection)

        assert result == ["gemini", "openai"]  # Sorted, unique

    def test_majority_provider_all_symbols(self):
        """Test finding majority provider across all symbols"""
        reflection = {
            "details": [
                {"symbol": "AAPL", "provider": "openai"},
                {"symbol": "TSLA", "provider": "openai"},
                {"symbol": "NVDA", "provider": "gemini"}
            ]
        }

        result = episode_store._majority_provider(reflection)

        assert result == "openai"  # 2 out of 3

    def test_majority_provider_single_symbol(self):
        """Test finding majority provider for specific symbol"""
        reflection = {
            "details": [
                {"symbol": "AAPL", "provider": "openai"},
                {"symbol": "AAPL", "provider": "openai"},
                {"symbol": "AAPL", "provider": "gemini"},
                {"symbol": "TSLA", "provider": "deepseek"}
            ]
        }

        result = episode_store._majority_provider(reflection, sym="AAPL")

        assert result == "openai"  # 2 out of 3 for AAPL


class TestBuildEpisode:
    """Test episode building"""

    def test_build_episode_full_data(self, temp_dir):
        """Test building episode with complete data"""
        runs_dir = temp_dir / "runs"
        runs_dir.mkdir()
        ref_dir = runs_dir / "2025-10-29"
        ref_dir.mkdir()

        reflection = {
            "aggregate": {"overall_consistency": 0.85}
        }
        ref_file = ref_dir / "reflection.json"
        ref_file.write_text(json.dumps(reflection))

        best = {
            "oos": {"oos_sharpe": 1.25, "oos_avg_max_drawdown": 0.08, "oos_trades": 45},
            "best_params": {"rl.enabled": True}
        }
        hpo_gate = {"ok": True}

        episode = episode_store.build_episode(
            runs_dir=str(runs_dir),
            session="morning",
            best=best,
            hpo_gate=hpo_gate,
            notes="Test episode",
            d="2025-10-29"
        )

        assert episode["date"] == "2025-10-29"
        assert episode["session"] == "morning"
        assert episode["oos_sharpe"] == 1.25
        assert episode["oos_mdd"] == 0.08
        assert episode["oos_trades"] == 45
        assert episode["consistency"] == 0.85
        assert episode["applied"] is True
        assert episode["notes"] == "Test episode"
        assert len(episode["params_id"]) == 40  # SHA1 hash

    def test_build_episode_fallback_keys(self, temp_dir):
        """Test building episode with alternative key names"""
        episode = episode_store.build_episode(
            runs_dir=str(temp_dir),
            session="intraday",
            best={
                "oos": {
                    "sharpe": 1.15,  # Alternative key
                    "max_drawdown": 0.10,  # Alternative key
                    "n": 30  # Alternative key
                }
            },
            d="2025-10-29"
        )

        assert episode["oos_sharpe"] == 1.15
        assert episode["oos_mdd"] == 0.10
        assert episode["oos_trades"] == 30

    def test_build_episode_missing_oos(self, temp_dir):
        """Test building episode with missing OOS data"""
        episode = episode_store.build_episode(
            runs_dir=str(temp_dir),
            session="evening",
            best={},
            d="2025-10-29"
        )

        assert episode["oos_sharpe"] == 0.0
        assert episode["oos_mdd"] == 0.0
        assert episode["oos_trades"] == 0

    def test_build_episode_gate_blocked(self, temp_dir):
        """Test building episode when HPO gate blocks"""
        episode = episode_store.build_episode(
            runs_dir=str(temp_dir),
            session="morning",
            hpo_gate={"ok": False, "reasons": ["oos_sharpe<1.1"]},
            d="2025-10-29"
        )

        assert episode["applied"] is False

    def test_build_episode_uses_today(self, temp_dir):
        """Test episode uses today's date when not provided"""
        with patch('datetime.date') as mock_date:
            mock_date.today.return_value = date(2025, 10, 29)

            episode = episode_store.build_episode(
                runs_dir=str(temp_dir),
                session="morning"
            )

            assert episode["date"] == "2025-10-29"


class TestWriteClickHouse:
    """Test ClickHouse persistence"""

    def test_write_clickhouse_creates_table(self, temp_dir):
        """Test table creation on first write"""
        episode = {
            "date": "2025-10-29",
            "session": "morning",
            "provider": "openai",
            "symbol": "AAPL",
            "sector": "Technology",
            "regime": "rvol_high|trend_up",
            "oos_sharpe": 1.25,
            "oos_mdd": 0.08,
            "oos_trades": 45,
            "consistency": 0.85,
            "applied": True,
            "params_id": "abc123",
            "notes": "Test",
            "created_at": "2025-10-29T10:00:00Z"
        }

        with patch('scripts.ai_trader.memory.episode_store.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_ch.return_value = mock_client

            episode_store.write_clickhouse(episode)

            # Verify table creation DDL was executed
            assert mock_client.execute.called
            ddl_call = mock_client.execute.call_args[0][0]
            assert "CREATE TABLE IF NOT EXISTS" in ddl_call
            assert "ai_trader_memory_episodes" in ddl_call

            # Verify insert was called
            assert mock_client.insert.called

    def test_write_clickhouse_no_client(self):
        """Test graceful handling when ClickHouse not available"""
        episode = {"date": "2025-10-29"}

        with patch('scripts.ai_trader.memory.episode_store.ClickHouseDB', None):
            # Should not raise error
            episode_store.write_clickhouse(episode)

    def test_write_clickhouse_row_format(self, temp_dir):
        """Test row data format matches schema"""
        episode = {
            "date": "2025-10-29",
            "session": "morning",
            "provider": "openai",
            "symbol": "AAPL",
            "sector": "Technology",
            "regime": "rvol_mid|trend_up",
            "oos_sharpe": 1.25,
            "oos_mdd": 0.08,
            "oos_trades": 45,
            "consistency": 0.85,
            "applied": True,
            "params_id": "abc123",
            "notes": "Test",
            "created_at": "2025-10-29T10:00:00Z"
        }

        with patch('scripts.ai_trader.memory.episode_store.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_ch.return_value = mock_client

            episode_store.write_clickhouse(episode)

            # Extract insert call
            insert_args = mock_client.insert.call_args
            row = insert_args[0][1][0]

            assert row[0] == "2025-10-29"  # date
            assert row[1] == "morning"  # session
            assert row[2] == "openai"  # provider
            assert row[6] == 1.25  # oos_sharpe
            assert row[10] == 1  # applied (converted to int)


class TestWritePostgres:
    """Test PostgreSQL persistence"""

    def test_write_postgres_creates_table(self):
        """Test table creation on first write"""
        episode = {
            "date": "2025-10-29",
            "session": "morning",
            "oos_sharpe": 1.25,
            "created_at": "2025-10-29T10:00:00Z"
        }

        with patch('scripts.ai_trader.memory.episode_store.PostgresDB') as mock_pg:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock()
            mock_client.execute = AsyncMock()
            mock_client.close = AsyncMock()
            mock_pg.return_value = mock_client

            episode_store.write_postgres(episode)

            # Verify table creation was called
            assert mock_client.execute.call_count >= 2  # CREATE + INSERT

    def test_write_postgres_no_client(self):
        """Test graceful handling when PostgreSQL not available"""
        episode = {"date": "2025-10-29"}

        with patch('scripts.ai_trader.memory.episode_store.PostgresDB', None):
            # Should not raise error
            episode_store.write_postgres(episode)

    def test_write_postgres_swallows_errors(self):
        """Test errors are swallowed to avoid blocking pipeline"""
        episode = {"date": "2025-10-29"}

        with patch('scripts.ai_trader.memory.episode_store.PostgresDB') as mock_pg:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(side_effect=Exception("Connection failed"))
            mock_pg.return_value = mock_client

            # Should not raise error
            episode_store.write_postgres(episode)


class TestStoreEpisode:
    """Test complete episode storage flow"""

    def test_store_episode_success(self, temp_dir):
        """Test successful episode storage"""
        runs_dir = temp_dir / "runs"
        runs_dir.mkdir()

        best = {
            "oos": {"oos_sharpe": 1.25, "oos_avg_max_drawdown": 0.08},
            "best_params": {"rl.enabled": True}
        }
        hpo_gate = {"ok": True}

        with patch('scripts.ai_trader.memory.episode_store.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_ch.return_value = mock_client

            result = episode_store.store_episode(
                runs_dir=str(runs_dir),
                session="morning",
                best=best,
                hpo_gate=hpo_gate,
                d="2025-10-29"
            )

            assert result["saved"] is True
            assert "save_error" not in result

    def test_store_episode_clickhouse_failure(self, temp_dir):
        """Test handling ClickHouse write failure"""
        with patch('scripts.ai_trader.memory.episode_store.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_client.execute.side_effect = Exception("CH connection failed")
            mock_ch.return_value = mock_client

            result = episode_store.store_episode(
                runs_dir=str(temp_dir),
                session="morning",
                d="2025-10-29"
            )

            assert result["saved"] is False
            assert "save_error" in result

    def test_store_episode_per_symbol_expansion(self, temp_dir):
        """Test episode is written per symbol with sector/regime"""
        runs_dir = temp_dir / "runs"
        runs_dir.mkdir()
        ref_dir = runs_dir / "2025-10-29"
        ref_dir.mkdir()

        reflection = {
            "details": [
                {"symbol": "AAPL", "provider": "openai"},
                {"symbol": "TSLA", "provider": "gemini"}
            ]
        }
        ref_file = ref_dir / "reflection.json"
        ref_file.write_text(json.dumps(reflection))

        with patch('scripts.ai_trader.memory.episode_store.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_client.query.return_value = [{"sector": "Technology"}]
            mock_ch.return_value = mock_client

            episode_store.store_episode(
                runs_dir=str(runs_dir),
                session="morning",
                d="2025-10-29"
            )

            # Should have 3 inserts: 1 base + 2 per-symbol
            assert mock_client.insert.call_count >= 3


class TestMemoryAggregator:
    """Test memory aggregation queries"""

    def test_top_providers_with_data(self):
        """Test top providers query with results"""
        mock_rows = [
            {"provider": "deepseek_nvidia", "avg_sharpe": 1.35, "avg_mdd": 0.07, "trades": 50},
            {"provider": "openai", "avg_sharpe": 1.20, "avg_mdd": 0.09, "trades": 45}
        ]

        with patch('scripts.ai_trader.memory.aggregator.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_client.query.return_value = mock_rows
            mock_ch.return_value = mock_client

            result = aggregator.top_providers("morning", lookback_days=14)

            assert len(result) == 2
            assert result[0]["provider"] == "deepseek_nvidia"
            assert result[0]["avg_sharpe"] == 1.35

    def test_top_providers_no_clickhouse(self):
        """Test graceful handling when ClickHouse not available"""
        with patch('scripts.ai_trader.memory.aggregator.ClickHouseDB', None):
            result = aggregator.top_providers("morning")

            assert result == []

    def test_top_by_sector_grouping(self):
        """Test top providers grouped by sector"""
        mock_rows = [
            {"sector": "Technology", "provider": "openai", "avg_sharpe": 1.30, "trades": 50},
            {"sector": "Technology", "provider": "gemini", "avg_sharpe": 1.20, "trades": 45},
            {"sector": "Finance", "provider": "deepseek", "avg_sharpe": 1.25, "trades": 40}
        ]

        with patch('scripts.ai_trader.memory.aggregator.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_client.query.return_value = mock_rows
            mock_ch.return_value = mock_client

            result = aggregator.top_by_sector("morning", top_k_per_sector=2)

            assert len(result) == 3

    def test_top_by_regime_filtering(self):
        """Test top providers grouped by market regime"""
        mock_rows = [
            {"regime": "rvol_high|trend_up", "provider": "openai", "avg_sharpe": 1.40, "trades": 50},
            {"regime": "rvol_low|trend_down", "provider": "gemini", "avg_sharpe": 1.10, "trades": 30}
        ]

        with patch('scripts.ai_trader.memory.aggregator.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_client.query.return_value = mock_rows
            mock_ch.return_value = mock_client

            result = aggregator.top_by_regime("intraday")

            assert len(result) == 2

    def test_top_symbols_ranking(self):
        """Test top symbols query"""
        mock_rows = [
            {"symbol": "AAPL", "sector": "Technology", "provider": "openai", "avg_sharpe": 1.50, "trades": 60},
            {"symbol": "TSLA", "sector": "Auto", "provider": "gemini", "avg_sharpe": 1.35, "trades": 55}
        ]

        with patch('scripts.ai_trader.memory.aggregator.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_client.query.return_value = mock_rows
            mock_ch.return_value = mock_client

            result = aggregator.top_symbols("morning", top_k=10)

            assert len(result) == 2
            assert result[0]["symbol"] == "AAPL"

    def test_memory_summary_full(self):
        """Test complete memory summary aggregation"""
        mock_top_providers = [{"provider": "openai", "avg_sharpe": 1.30}]
        mock_top_sector = [{"sector": "Tech", "provider": "openai", "avg_sharpe": 1.25}]
        mock_top_regime = [{"regime": "rvol_high", "provider": "gemini", "avg_sharpe": 1.35}]
        mock_top_symbols = [{"symbol": "AAPL", "provider": "openai", "avg_sharpe": 1.40}]

        with patch('scripts.ai_trader.memory.aggregator.ClickHouseDB') as mock_ch:
            mock_client = MagicMock()
            mock_client.query.side_effect = [
                mock_top_providers,
                mock_top_sector,
                mock_top_regime,
                mock_top_symbols
            ]
            mock_ch.return_value = mock_client

            result = aggregator.memory_summary("morning", lookback_days=14)

            assert result["session"] == "morning"
            assert "as_of" in result
            assert len(result["top_providers"]) == 1
            assert len(result["top_by_sector"]) == 1
            assert len(result["top_by_regime"]) == 1
            assert len(result["top_symbols"]) == 1
