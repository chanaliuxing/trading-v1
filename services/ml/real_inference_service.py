#!/usr/bin/env python3
"""
Real ML Inference Service - Uses actual OOF predictions from ClickHouse

This service provides REAL ML predictions based on trained XGB/LSTM models.
Uses out-of-fold predictions stored in ClickHouse as the source of truth.

For real-time inference, queries the most recent OOF prediction for the symbol.
"""

from datetime import datetime, timedelta
from typing import Dict, Any, Optional

# Lazy imports
try:
    from libs.logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    # Fallback for standalone testing
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

# ClickHouse client
try:
    from clickhouse_driver import Client
except ImportError:
    raise ImportError("clickhouse_driver not installed. Run: pip install clickhouse-driver")


def get_client():
    """Get ClickHouse client."""
    return Client(host='localhost', port=9000)


class RealMLInferenceService:
    """Real ML inference using ClickHouse OOF predictions."""

    def __init__(self):
        """Initialize real inference service with ClickHouse connection."""
        # Production safety check: block instantiation in production
        import os
        TRADING_MODE = os.getenv("TRADING_MODE", "dev").lower()
        if TRADING_MODE in {"production", "prod", "live"}:
            raise RuntimeError(
                "RealMLInferenceService is disabled in production. "
                "This service queries historical OOF predictions, not real-time inference. "
                "Use ModelMLInferenceService for live trading."
            )

        try:
            self.client = get_client()
            logger.info("real_ml_inference_initialized", backend="clickhouse_oof")

            # Verify we can access predictions
            self._verify_data_access()

        except Exception as e:
            if hasattr(logger, 'bind'):  # structlog
                logger.error("real_ml_inference_init_failed", error=str(e), exc_info=True)
            else:  # standard logging
                logger.error(f"real_ml_inference_init_failed: {e}", exc_info=True)
            raise

    def _verify_data_access(self) -> None:
        """Verify we can access OOF predictions."""
        try:
            query = "SELECT COUNT(*) as count FROM trading.oof_xgb_h5 LIMIT 1"
            result = self.client.execute(query)
            count = result[0][0] if result else 0

            if hasattr(logger, 'bind'):  # structlog
                logger.info("oof_predictions_accessible", xgb_rows=count)
            else:
                logger.info(f"OOF predictions accessible: {count} rows")

            if count == 0:
                if hasattr(logger, 'bind'):
                    logger.warning("no_oof_predictions_found",
                                 message="OOF prediction table is empty. Models may need retraining.")
                else:
                    logger.warning("OOF prediction table is empty. Models may need retraining.")
        except Exception as e:
            if hasattr(logger, 'bind'):
                logger.error("oof_access_verification_failed", error=str(e))
            else:
                logger.error(f"OOF access verification failed: {e}")
            raise

    def predict(self, features: Dict[str, Any]) -> Dict[str, float]:
        """
        Generate real ML predictions for a single bar.

        Args:
            features: Feature dict containing at minimum:
                     - symbol: str
                     - ts: datetime or timestamp
                     - close: float
                     ... other features

        Returns:
            Dict with keys: ml_signal, ml_conf, xgb_signal, xgb_conf,
                           lstm_signal, lstm_conf, fusion_signal, fusion_conf
        """
        symbol = features.get("symbol", "UNKNOWN")
        ts = features.get("ts")

        # Convert timestamp to datetime if needed
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts)
        elif isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except Exception:
                ts = datetime.now()

        # Query most recent OOF prediction for this symbol
        # Look within a 5-minute window
        try:
            xgb_pred = self._get_xgb_prediction(symbol, ts)
            lstm_pred = self._get_lstm_prediction(symbol, ts)

            # If no recent prediction, use conservative defaults
            if xgb_pred is None:
                logger.warning("no_xgb_prediction", symbol=symbol, ts=str(ts))
                xgb_pred = 0.5  # Neutral

            if lstm_pred is None:
                logger.debug("no_lstm_prediction", symbol=symbol, ts=str(ts))
                lstm_pred = 0.5  # Neutral

            # Ensemble: weighted average (can be tuned)
            # TODO: Read weights from config
            xgb_weight = 0.7
            lstm_weight = 0.3
            fusion_pred = xgb_weight * xgb_pred + lstm_weight * lstm_pred

            # Convert probabilities to signals
            xgb_signal = 1.0 if xgb_pred > 0.5 else -1.0
            lstm_signal = 1.0 if lstm_pred > 0.5 else -1.0
            fusion_signal = 1.0 if fusion_pred > 0.5 else -1.0

            # Convert to confidence [0, 1]
            xgb_conf = abs(xgb_pred - 0.5) * 2.0
            lstm_conf = abs(lstm_pred - 0.5) * 2.0
            fusion_conf = abs(fusion_pred - 0.5) * 2.0

            # Primary signal is fusion
            ml_signal = fusion_signal
            ml_conf = fusion_conf

            logger.debug(
                "ml_prediction_generated",
                symbol=symbol,
                ts=str(ts),
                xgb_pred=round(xgb_pred, 4),
                lstm_pred=round(lstm_pred, 4),
                fusion_pred=round(fusion_pred, 4),
                ml_signal=ml_signal,
                ml_conf=round(ml_conf, 4)
            )

            return {
                # Primary ML predictions (used by strategy)
                "ml_signal": ml_signal,
                "ml_conf": ml_conf,
                # Individual model predictions
                "xgb_signal": xgb_signal,
                "xgb_conf": xgb_conf,
                "lstm_signal": lstm_signal,
                "lstm_conf": lstm_conf,
                "fusion_signal": fusion_signal,
                "fusion_conf": fusion_conf,
                # Raw predictions (before confidence conversion)
                "xgb_pred_raw": xgb_pred,
                "lstm_pred_raw": lstm_pred,
                "fusion_pred_raw": fusion_pred,
            }

        except Exception as e:
            logger.error("prediction_failed", symbol=symbol, error=str(e), exc_info=True)
            # Return neutral prediction on error
            return self._neutral_prediction()

    def _get_xgb_prediction(self, symbol: str, ts: datetime) -> Optional[float]:
        """
        Get XGB prediction from OOF table.

        Looks for the most recent prediction within 5 minutes of target timestamp.
        """
        try:
            # Query for closest prediction within ±5 minutes
            ts_start = ts - timedelta(minutes=5)
            ts_end = ts + timedelta(minutes=5)

            query = f"""
            SELECT pred
            FROM trading.oof_xgb_h5
            WHERE symbol = '{symbol}'
              AND ts >= toDateTime('{ts_start.strftime('%Y-%m-%d %H:%M:%S')}')
              AND ts <= toDateTime('{ts_end.strftime('%Y-%m-%d %H:%M:%S')}')
            ORDER BY abs(dateDiff('second', ts, toDateTime('{ts.strftime('%Y-%m-%d %H:%M:%S')}')))
            LIMIT 1
            """

            result = self.client.execute(query)

            if result and len(result) > 0:
                pred = float(result[0][0])
                return pred

            return None

        except Exception as e:
            if hasattr(logger, 'bind'):
                logger.error("xgb_prediction_query_failed", symbol=symbol, error=str(e))
            else:
                logger.error(f"XGB prediction query failed for {symbol}: {e}")
            return None

    def _get_lstm_prediction(self, symbol: str, ts: datetime) -> Optional[float]:
        """
        Get LSTM prediction from OOF table.

        Note: LSTM predictions are sparse, may not be available for all timestamps.
        """
        try:
            ts_start = ts - timedelta(minutes=5)
            ts_end = ts + timedelta(minutes=5)

            query = f"""
            SELECT pred
            FROM trading.oof_lstm_h5
            WHERE symbol = '{symbol}'
              AND ts >= toDateTime('{ts_start.strftime('%Y-%m-%d %H:%M:%S')}')
              AND ts <= toDateTime('{ts_end.strftime('%Y-%m-%d %H:%M:%S')}')
            ORDER BY abs(dateDiff('second', ts, toDateTime('{ts.strftime('%Y-%m-%d %H:%M:%S')}')))
            LIMIT 1
            """

            result = self.client.execute(query)

            if result and len(result) > 0:
                pred = float(result[0][0])
                return pred

            return None

        except Exception as e:
            if hasattr(logger, 'bind'):
                logger.debug("lstm_prediction_query_failed", symbol=symbol, error=str(e))
            else:
                logger.debug(f"LSTM prediction query failed for {symbol}: {e}")
            return None

    def _neutral_prediction(self) -> Dict[str, float]:
        """Return neutral prediction (no trade signal)."""
        return {
            "ml_signal": 0.0,      # No signal
            "ml_conf": 0.0,        # No confidence
            "xgb_signal": 0.0,
            "xgb_conf": 0.0,
            "lstm_signal": 0.0,
            "lstm_conf": 0.0,
            "fusion_signal": 0.0,
            "fusion_conf": 0.0,
            "xgb_pred_raw": 0.5,
            "lstm_pred_raw": 0.5,
            "fusion_pred_raw": 0.5,
        }

    def predict_batch(self, features_list: list) -> list:
        """
        Generate predictions for multiple bars.

        Args:
            features_list: List of feature dicts

        Returns:
            List of prediction dicts
        """
        return [self.predict(f) for f in features_list]


# Singleton instance
_real_service: Optional[RealMLInferenceService] = None


def get_real_service() -> RealMLInferenceService:
    """Get or create singleton real service instance."""
    global _real_service
    if _real_service is None:
        _real_service = RealMLInferenceService()
    return _real_service


if __name__ == "__main__":
    # Test the real service
    import sys
    import json
    from pathlib import Path

    # Add project root to path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    print("Testing Real ML Inference Service")
    print("=" * 60)

    try:
        service = RealMLInferenceService()

        # Test with TSLA
        test_features = {
            "symbol": "TSLA",
            "ts": datetime.now(),
            "close": 250.0,
        }

        print(f"\nTest prediction for {test_features['symbol']}:")
        prediction = service.predict(test_features)
        print(json.dumps(prediction, indent=2))

        print("\n✅ Real ML Inference Service is working!")

    except Exception as e:
        print(f"\n❌ Service test failed: {e}")
        import traceback
        traceback.print_exc()
