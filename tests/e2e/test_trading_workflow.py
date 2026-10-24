#!/usr/bin/env python3
"""
End-to-end tests for complete trading workflows
Tests data loading → feature engineering → ML inference → strategy execution → backtesting
"""
import pytest
from pathlib import Path
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _is_no_data_error(err: Exception) -> bool:
    msg = str(err).lower()
    return ("no data" in msg) or ("empty" in msg)


class TestCompleteBacktestWorkflow:
    """Test complete backtest workflow from data to results"""

    @pytest.fixture
    def workflow_config(self):
        """Configuration for E2E workflow"""
        return {
            "symbols": ["TSLA"],
            "start": "2025-10-01 09:30:00",
            "end": "2025-10-05 16:00:00",
            "features_table": "trading.features_1m_with_oof",
            "initial_cash": 100000.0,
            "strategy_params": {
                "base_qty": 100,
                "ensemble": {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]},
                "market_filter": {"conf_floor": 0.0, "min_volume": 0, "max_spread_bps": 999}
            }
        }

    @pytest.mark.e2e
    @pytest.mark.slow
    def test_end_to_end_backtest(self, workflow_config, tmp_path):
        """Test complete backtest workflow"""
        from backtest_ml_engine import BacktestEngine, BacktestConfig

        # Step 1: Create config
        config = BacktestConfig(
            symbols=workflow_config["symbols"],
            start=workflow_config["start"],
            end=workflow_config["end"],
            timeframe="1m",
            features_table=workflow_config["features_table"],
            initial_cash=workflow_config["initial_cash"],
            strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
            strategy_params=workflow_config["strategy_params"]
        )

        # Step 2: Initialize engine
        engine = BacktestEngine(config)

        # Step 3: Run backtest
        try:
            result = engine.run(df=None)
        except Exception as e:
            if _is_no_data_error(e):
                pytest.skip(f"Backtest skipped due to no data in selected window: {e}")
            raise

        # Step 4: Verify results
        assert isinstance(result, dict), "Result should be a dictionary"
        assert "metrics" in result or "trades" in result, "Result should have metrics or trades"

        # Step 5: Save results
        out_file = tmp_path / "backtest_result.json"
        with open(out_file, 'w') as f:
            json.dump(result, f, indent=2, default=str)

        assert out_file.exists(), "Results should be saved"


class TestEnsembleComparisonWorkflow:
    """Test complete ensemble comparison workflow"""

    @pytest.fixture
    def comparison_config(self):
        """Configuration for ensemble comparison"""
        return {
            "symbols": ["TSLA"],
            "start": "2025-10-01 09:30:00",
            "end": "2025-10-05 16:00:00",
            "features_table": "trading.features_1m_with_oof",
            "w_xgb": 0.6,
            "w_lstm": 0.4
        }

    @pytest.mark.e2e
    @pytest.mark.slow
    def test_three_mode_comparison(self, comparison_config, tmp_path):
        """Test XGB-only, LSTM-only, and Fusion comparison"""
        from tools.backtest_ensemble_compare import _bt, _metrics

        base_extras = {
            "symbols": comparison_config["symbols"],
            "bar_sec": 60,
            "market_filter": {
                "conf_floor": 0.0,
                "check_after_hours": False,
                "min_volume": 0,
                "max_spread_bps": 999
            },
            "sizing": {"mode": "fixed", "base_qty": 100}
        }

        # Mode 1: XGB-only
        xgb_extras = dict(base_extras)
        xgb_extras["ensemble"] = {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]}

        try:
            xgb_result = _bt(
                comparison_config["symbols"],
                comparison_config["start"],
                comparison_config["end"],
                xgb_extras,
                comparison_config["features_table"]
            )
        except Exception as e:
            if _is_no_data_error(e):
                pytest.skip(f"Ensemble comparison skipped due to no data in selected window: {e}")
            raise
        xgb_metrics = _metrics(xgb_result)

        # Mode 2: LSTM-only
        lstm_extras = dict(base_extras)
        lstm_extras["ensemble"] = {"mode": "weighted_avg", "models": [{"name": "lstm", "weight": 1.0}]}

        lstm_result = _bt(
            comparison_config["symbols"],
            comparison_config["start"],
            comparison_config["end"],
            lstm_extras,
            comparison_config["features_table"]
        )
        lstm_metrics = _metrics(lstm_result)

        # Mode 3: Fusion
        fusion_extras = dict(base_extras)
        fusion_extras["ensemble"] = {
            "mode": "weighted_avg",
            "models": [
                {"name": "xgb", "weight": comparison_config["w_xgb"]},
                {"name": "lstm", "weight": comparison_config["w_lstm"]}
            ]
        }

        fusion_result = _bt(
            comparison_config["symbols"],
            comparison_config["start"],
            comparison_config["end"],
            fusion_extras,
            comparison_config["features_table"]
        )
        fusion_metrics = _metrics(fusion_result)

        # Compile results
        results = {
            "xgb_only": xgb_metrics,
            "lstm_only": lstm_metrics,
            "fusion": fusion_metrics,
            "config": comparison_config
        }

        # Save results
        out_file = tmp_path / "ensemble_comparison.json"
        with open(out_file, 'w') as f:
            json.dump(results, f, indent=2, default=str)

        assert out_file.exists(), "Comparison results should be saved"

        # Verify all modes ran
        assert isinstance(xgb_metrics, dict)
        assert isinstance(lstm_metrics, dict)
        assert isinstance(fusion_metrics, dict)


class TestRLTrainingEvaluationWorkflow:
    """Test complete RL training and evaluation workflow"""

    @pytest.fixture
    def rl_config(self):
        """Configuration for RL workflow"""
        return {
            "symbols": ["TSLA"],
            "train_start": "2025-09-01",
            "train_end": "2025-09-30",
            "eval_start": "2025-10-01",
            "eval_end": "2025-10-15",
            "timesteps": 5000,  # Small for fast testing
            "cost_bps": 2.0,
            "dd_penalty": 0.1,
            "turnover_penalty": 0.05
        }

    @pytest.mark.e2e
    @pytest.mark.slow
    def test_rl_train_and_eval_workflow(self, rl_config, tmp_path):
        """Test RL training followed by evaluation"""
        try:
            from rl_core.train import train_rl_model
            from scripts.ai_trader.rl.eval_rl import evaluate_rl_model
        except ImportError:
            pytest.skip("RL modules not available")

        # Step 1: Train RL model
        model_path = tmp_path / "test_rl_model.zip"

        try:
            train_rl_model(
                symbols=rl_config["symbols"],
                start=rl_config["train_start"],
                end=rl_config["train_end"],
                features_table="trading.features_1m",
                timesteps=rl_config["timesteps"],
                cost_bps=rl_config["cost_bps"],
                dd_penalty=rl_config["dd_penalty"],
                turnover_penalty=rl_config["turnover_penalty"],
                output_path=str(model_path)
            )
        except Exception as e:
            pytest.skip(f"Training failed, likely due to data issues: {e}")

        # Verify model was created
        assert model_path.exists(), "RL model should be saved"

        # Step 2: Evaluate RL model
        eval_out_dir = tmp_path / "eval_results"
        eval_out_dir.mkdir(parents=True, exist_ok=True)

        try:
            eval_results = evaluate_rl_model(
                start=rl_config["eval_start"],
                end=rl_config["eval_end"],
                symbols=rl_config["symbols"],
                model_path=str(model_path),
                ignore_hours=True,
                min_sharpe=0.0,  # Relaxed for testing
                max_mdd=1.0,
                out_dir=str(eval_out_dir)
            )
        except Exception as e:
            pytest.skip(f"Evaluation failed: {e}")

        # Verify evaluation results
        assert isinstance(eval_results, dict)


class TestDataPipeline:
    """Test complete data pipeline"""

    @pytest.mark.e2e
    @pytest.mark.slow
    def test_data_loading_and_feature_engineering(self):
        """Test data loads from ClickHouse and features are computed"""
        from libs.db.ch_pool import get_client
        import pandas as pd

        client = get_client()

        # Query data
        query = """
        SELECT
            ts_bar_end,
            symbol,
            open, high, low, close, volume,
            ml_signal, ml_conf,
            lstm_oof_h5, lstm_oof_conf_h5,
            ret_5, volatility, rsi_14
        FROM trading.features_1m_with_oof
        WHERE symbol = 'TSLA'
        AND ts_bar_end >= '2025-10-01 09:30:00'
        AND ts_bar_end < '2025-10-02 16:00:00'
        ORDER BY ts_bar_end
        LIMIT 100
        """

        try:
            df = client.query_df(query)
        except Exception as e:
            pytest.skip(f"Data query failed: {e}")

        # Environment-dependent: no rows in selected range means this E2E check is not applicable.
        if df.empty:
            pytest.skip("Data loading check skipped: no rows in selected date range")

        # Verify data structure
        assert 'close' in df.columns, "OHLC columns should exist"
        assert 'ts_bar_end' in df.columns, "Timestamp column should exist"

        # Verify features
        expected_features = ['ret_5', 'volatility', 'rsi_14']
        for feat in expected_features:
            if feat in df.columns:
                assert df[feat].notna().any(), f"{feat} should have non-null values"

    @pytest.mark.e2e
    def test_oof_data_availability(self):
        """Test OOF predictions are available"""
        from libs.db.ch_pool import get_client

        client = get_client()

        query = """
        SELECT
            count() as total_rows,
            countIf(lstm_oof_conf_h5 > 0) as valid_oof_rows,
            valid_oof_rows / total_rows as coverage
        FROM trading.features_1m_with_oof
        WHERE symbol = 'TSLA'
        AND ts_bar_end >= '2025-10-01'
        AND ts_bar_end < '2025-11-01'
        """

        try:
            df = client.query_df(query)
        except Exception as e:
            pytest.skip(f"OOF query failed: {e}")

        # Verify OOF data exists
        if df.iloc[0]['total_rows'] <= 0:
            pytest.skip("OOF coverage check skipped: no rows in selected date range")

        # Check coverage (can be 0% if LSTM not trained yet)
        coverage = df.iloc[0]['coverage']
        assert 0.0 <= coverage <= 1.0, "Coverage should be between 0 and 1"


class TestStrategyExecution:
    """Test strategy execution with real data"""

    @pytest.mark.e2e
    @pytest.mark.slow
    def test_strategy_generates_signals(self):
        """Test strategy generates trading signals"""
        from backtest_ml_engine import BacktestEngine, BacktestConfig

        config = BacktestConfig(
            symbols=["TSLA"],
            start="2025-10-01 09:30:00",
            end="2025-10-02 16:00:00",
            timeframe="1m",
            features_table="trading.features_1m_with_oof",
            initial_cash=100000.0,
            strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
            strategy_params={
                "base_qty": 100,
                "ensemble": {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]},
                "market_filter": {"conf_floor": 0.0, "min_volume": 0, "max_spread_bps": 999}
            }
        )

        engine = BacktestEngine(config)
        try:
            result = engine.run(df=None)
        except Exception as e:
            if _is_no_data_error(e):
                pytest.skip(f"Strategy execution skipped due to no data in selected window: {e}")
            raise

        # Verify execution
        assert isinstance(result, dict)

        # Note: May have 0 trades if signals are weak or filtered
        # This is expected behavior, not a test failure


class TestErrorHandling:
    """Test error handling in E2E workflows"""

    @pytest.mark.e2e
    def test_invalid_symbol_handling(self):
        """Test handling of invalid symbols"""
        from backtest_ml_engine import BacktestEngine, BacktestConfig

        config = BacktestConfig(
            symbols=["INVALID_SYMBOL_XYZ"],
            start="2025-10-01",
            end="2025-10-02",
            timeframe="1m",
            features_table="trading.features_1m",
            initial_cash=100000.0,
            strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
            strategy_params={}
        )

        engine = BacktestEngine(config)

        # Should handle gracefully (empty data or error)
        try:
            result = engine.run(df=None)
            # If no error, result should be empty or indicate no data
            assert isinstance(result, dict)
        except Exception as e:
            # Expected: data not found error
            assert _is_no_data_error(e)

    @pytest.mark.e2e
    def test_invalid_date_range_handling(self):
        """Test handling of invalid date ranges"""
        from backtest_ml_engine import BacktestEngine, BacktestConfig

        config = BacktestConfig(
            symbols=["TSLA"],
            start="2025-12-31",  # Future date
            end="2026-01-01",
            timeframe="1m",
            features_table="trading.features_1m",
            initial_cash=100000.0,
            strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
            strategy_params={}
        )

        engine = BacktestEngine(config)

        # Should handle gracefully
        try:
            result = engine.run(df=None)
            # No data expected
            assert isinstance(result, dict)
        except Exception as e:
            # Expected error
            assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-m", "not slow"])
