#!/usr/bin/env python3
"""
Comprehensive Data Quality Validation Tool
全面的数据质量检查工具

Features:
1. Data Completeness - 数据完整性检查
2. Data Continuity - 数据连续性检查
3. Feature Validation - 特征计算验证
4. Data Accuracy - 数据准确性验证
5. Performance Monitoring - 性能监控
6. Real-time Data Checks - 实时数据检查

Usage:
    # Full daily validation
    python tools/comprehensive_data_validation.py --date 2025-10-27 --symbols AAPL,TSLA,NVDA --full

    # Quick check (only critical items)
    python tools/comprehensive_data_validation.py --symbols AAPL,TSLA --quick

    # Specific checks
    python tools/comprehensive_data_validation.py --symbols AAPL --checks continuity,features

    # Generate JSON report
    python tools/comprehensive_data_validation.py --symbols AAPL --output reports/data_quality.json
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from zoneinfo import ZoneInfo

import clickhouse_connect
import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from libs.logger import get_logger

logger = get_logger(__name__)

# Configuration
CH_HOST = os.getenv("CH_HOST", "localhost")
CH_PORT = int(os.getenv("CH_PORT", 8123))
CH_DB = os.getenv("CH_DB", "trading")
CH_USER = os.getenv("CH_USER", "default")
CH_PASSWORD = os.getenv("CH_PASSWORD", "")

ET = ZoneInfo("America/New_York")
RTH_START = time(9, 30)  # 09:30 ET
RTH_END = time(16, 0)    # 16:00 ET


class DataValidator:
    """Comprehensive data quality validator"""

    def __init__(self, symbols: List[str], date: str, verbose: bool = False):
        self.symbols = symbols
        self.date = date
        self.verbose = verbose
        self.client = None
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "date": date,
            "symbols": symbols,
            "checks": {},
            "summary": {
                "total_checks": 0,
                "passed": 0,
                "warnings": 0,
                "errors": 0,
                "critical_errors": 0
            }
        }

    def connect(self) -> bool:
        """Connect to ClickHouse"""
        try:
            self.client = clickhouse_connect.get_client(
                host=CH_HOST,
                port=CH_PORT,
                database=CH_DB,
                user=CH_USER,
                password=CH_PASSWORD,
            )
            self.client.command("SELECT 1")
            logger.info(f"✅ Connected to ClickHouse at {CH_HOST}:{CH_PORT}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to connect to ClickHouse: {e}")
            return False

    def log(self, level: str, check: str, message: str, **kwargs):
        """Log check result"""
        if level == "info":
            logger.info(message, check=check, **kwargs)
        elif level == "warning":
            logger.warning(message, check=check, **kwargs)
            self.results["summary"]["warnings"] += 1
        elif level == "error":
            logger.error(message, check=check, **kwargs)
            self.results["summary"]["errors"] += 1
        elif level == "critical":
            logger.error(f"🔴 CRITICAL: {message}", check=check, **kwargs)
            self.results["summary"]["critical_errors"] += 1

    # =========================================================================
    # 1. DATA COMPLETENESS CHECKS - 数据完整性检查
    # =========================================================================

    def check_bar_completeness(self, symbol: str, resolution: str = "1m") -> Dict[str, Any]:
        """
        Check if bar data is complete for a trading day
        检查 K 线数据是否完整
        """
        check_name = f"bar_completeness_{resolution}"
        result = {
            "status": "unknown",
            "expected": 0,
            "actual": 0,
            "missing": 0,
            "completeness_pct": 0.0,
            "message": ""
        }

        table_map = {
            "1m": "bars_1m_ibkr",
            "5m": "bars_5m_ibkr",
            "15m": "bars_15m_ibkr",
            "1h": "bars_1h_ibkr",
            "1d": "bars_1d_ibkr"
        }

        table = table_map.get(resolution, "bars_1m_ibkr")

        # Expected bar counts (RTH only)
        expected_counts = {
            "1m": 390,    # 9:30-16:00 = 390 minutes
            "5m": 78,     # 390 / 5
            "15m": 26,    # 390 / 15
            "1h": 7,      # 9:30-16:30 (7 bars: 9:30, 10:30, ..., 15:30)
            "1d": 1
        }

        expected = expected_counts.get(resolution, 390)
        result["expected"] = expected

        query = f"""
        SELECT count() as bar_count
        FROM {table}
        WHERE symbol = %(symbol)s
          AND toDate(ts_start) = toDate(%(date)s)
          AND toHour(ts_start) >= 9
          AND toHour(ts_start) < 16
        """

        try:
            res = self.client.query(query, parameters={'symbol': symbol, 'date': self.date})
            actual = res.result_rows[0][0] if res.result_rows else 0
            result["actual"] = actual
            result["missing"] = max(0, expected - actual)
            result["completeness_pct"] = round((actual / expected) * 100, 2) if expected > 0 else 0

            if actual >= expected:
                result["status"] = "pass"
                result["message"] = f"Complete: {actual}/{expected} bars"
                self.log("info", check_name, f"✅ {symbol} {resolution}: {result['message']}")
            elif actual >= expected * 0.95:
                result["status"] = "warning"
                result["message"] = f"Nearly complete: {actual}/{expected} bars ({result['completeness_pct']}%)"
                self.log("warning", check_name, f"⚠️ {symbol} {resolution}: {result['message']}")
            elif actual > 0:
                result["status"] = "error"
                result["message"] = f"Incomplete: {actual}/{expected} bars ({result['completeness_pct']}%)"
                self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")
            else:
                result["status"] = "critical"
                result["message"] = "No data found"
                self.log("critical", check_name, f"🔴 {symbol} {resolution}: {result['message']}")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Query failed: {str(e)}"
            self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")

        return result

    def check_tick_data_volume(self, symbol: str) -> Dict[str, Any]:
        """
        Check if tick data volume is reasonable for a trading day
        检查 Tick 数据量是否合理
        """
        check_name = "tick_data_volume"
        result = {
            "status": "unknown",
            "trades_count": 0,
            "quotes_count": 0,
            "message": ""
        }

        # Check trades
        query_trades = """
        SELECT count() as cnt
        FROM lake.ticks_trades
        WHERE symbol = %(symbol)s
          AND toDate(ts) = toDate(%(date)s)
        """

        # Check quotes
        query_quotes = """
        SELECT count() as cnt
        FROM lake.quotes_l1
        WHERE symbol = %(symbol)s
          AND toDate(ts) = toDate(%(date)s)
        """

        try:
            trades_res = self.client.query(query_trades, parameters={'symbol': symbol, 'date': self.date})
            trades_count = trades_res.result_rows[0][0] if trades_res.result_rows else 0
            result["trades_count"] = trades_count

            quotes_res = self.client.query(query_quotes, parameters={'symbol': symbol, 'date': self.date})
            quotes_count = quotes_res.result_rows[0][0] if quotes_res.result_rows else 0
            result["quotes_count"] = quotes_count

            # Heuristic: Active stocks should have at least 10k trades per day
            if trades_count >= 10000:
                result["status"] = "pass"
                result["message"] = f"Good volume: {trades_count:,} trades, {quotes_count:,} quotes"
                self.log("info", check_name, f"✅ {symbol}: {result['message']}")
            elif trades_count >= 1000:
                result["status"] = "warning"
                result["message"] = f"Low volume: {trades_count:,} trades, {quotes_count:,} quotes"
                self.log("warning", check_name, f"⚠️ {symbol}: {result['message']}")
            elif trades_count > 0:
                result["status"] = "error"
                result["message"] = f"Very low volume: {trades_count:,} trades"
                self.log("error", check_name, f"❌ {symbol}: {result['message']}")
            else:
                result["status"] = "critical"
                result["message"] = "No tick data found"
                self.log("critical", check_name, f"🔴 {symbol}: {result['message']}")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Query failed: {str(e)}"
            self.log("error", check_name, f"❌ {symbol}: {result['message']}")

        return result

    # =========================================================================
    # 2. DATA CONTINUITY CHECKS - 数据连续性检查
    # =========================================================================

    def check_time_gaps(self, symbol: str, resolution: str = "1m") -> Dict[str, Any]:
        """
        Check for unexpected time gaps in bar data
        检查时间序列中的异常间隔
        """
        check_name = f"time_gaps_{resolution}"
        result = {
            "status": "unknown",
            "gaps_found": 0,
            "max_gap_minutes": 0,
            "gaps": [],
            "message": ""
        }

        table_map = {
            "1m": "bars_1m_ibkr",
            "5m": "bars_5m_ibkr",
            "15m": "bars_15m_ibkr"
        }
        table = table_map.get(resolution, "bars_1m_ibkr")

        interval_minutes = {
            "1m": 1,
            "5m": 5,
            "15m": 15
        }
        expected_interval = interval_minutes.get(resolution, 1)

        query = f"""
        WITH ordered_bars AS (
            SELECT
                ts_start,
                lagInFrame(ts_start) OVER (ORDER BY ts_start) AS prev_ts
            FROM {table}
            WHERE symbol = %(symbol)s
              AND toDate(ts_start) = toDate(%(date)s)
              AND toHour(ts_start) >= 9
              AND toHour(ts_start) < 16
            ORDER BY ts_start
        )
        SELECT
            prev_ts,
            ts_start,
            dateDiff('minute', prev_ts, ts_start) AS gap_minutes
        FROM ordered_bars
        WHERE prev_ts IS NOT NULL
          AND gap_minutes > %(expected)s
        ORDER BY gap_minutes DESC
        LIMIT 10
        """

        try:
            res = self.client.query(query, parameters={
                'symbol': symbol,
                'date': self.date,
                'expected': expected_interval
            })

            gaps = []
            for row in res.result_rows:
                prev_ts, curr_ts, gap_min = row
                gaps.append({
                    "from": str(prev_ts),
                    "to": str(curr_ts),
                    "gap_minutes": gap_min
                })

            result["gaps_found"] = len(gaps)
            result["gaps"] = gaps[:5]  # Top 5 largest gaps
            result["max_gap_minutes"] = gaps[0]["gap_minutes"] if gaps else 0

            if result["gaps_found"] == 0:
                result["status"] = "pass"
                result["message"] = "No unexpected gaps"
                self.log("info", check_name, f"✅ {symbol} {resolution}: {result['message']}")
            elif result["gaps_found"] <= 3:
                result["status"] = "warning"
                result["message"] = f"Found {result['gaps_found']} small gaps (max: {result['max_gap_minutes']} min)"
                self.log("warning", check_name, f"⚠️ {symbol} {resolution}: {result['message']}")
            else:
                result["status"] = "error"
                result["message"] = f"Found {result['gaps_found']} gaps (max: {result['max_gap_minutes']} min)"
                self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Query failed: {str(e)}"
            self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")

        return result

    def check_duplicate_bars(self, symbol: str, resolution: str = "1m") -> Dict[str, Any]:
        """
        Check for duplicate timestamps in bar data
        检查是否有重复的时间戳
        """
        check_name = f"duplicate_bars_{resolution}"
        result = {
            "status": "unknown",
            "duplicates_found": 0,
            "duplicate_timestamps": [],
            "message": ""
        }

        table_map = {
            "1m": "bars_1m_ibkr",
            "5m": "bars_5m_ibkr",
            "15m": "bars_15m_ibkr"
        }
        table = table_map.get(resolution, "bars_1m_ibkr")

        query = f"""
        SELECT
            ts_start,
            count() as cnt
        FROM {table}
        WHERE symbol = %(symbol)s
          AND toDate(ts_start) = toDate(%(date)s)
        GROUP BY ts_start
        HAVING cnt > 1
        ORDER BY cnt DESC
        LIMIT 10
        """

        try:
            res = self.client.query(query, parameters={'symbol': symbol, 'date': self.date})
            duplicates = [{"timestamp": str(row[0]), "count": row[1]} for row in res.result_rows]

            result["duplicates_found"] = len(duplicates)
            result["duplicate_timestamps"] = duplicates

            if result["duplicates_found"] == 0:
                result["status"] = "pass"
                result["message"] = "No duplicates found"
                self.log("info", check_name, f"✅ {symbol} {resolution}: {result['message']}")
            else:
                result["status"] = "error"
                result["message"] = f"Found {result['duplicates_found']} duplicate timestamps"
                self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Query failed: {str(e)}"
            self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")

        return result

    # =========================================================================
    # 3. FEATURE VALIDATION - 特征计算验证
    # =========================================================================

    def check_feature_completeness(self, symbol: str) -> Dict[str, Any]:
        """
        Check if calculated features exist for all bars
        检查特征是否已计算完整
        """
        check_name = "feature_completeness"
        result = {
            "status": "unknown",
            "bars_total": 0,
            "features_available": 0,
            "coverage_pct": 0.0,
            "message": ""
        }

        # Count bars in bars_1m_ibkr
        query_bars = """
        SELECT count() as cnt
        FROM bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND toDate(ts_start) = toDate(%(date)s)
        """

        # Count features in features_1m table (check if any feature is populated)
        # Note: close/vwap/volume are always populated, so we check for calculated features
        # Updated to use current column names: ema_9, ema_21, zscore_close_20 (not legacy ema_10/ema_20/z_price_20)
        query_features = """
        SELECT count() as cnt
        FROM features_1m
        WHERE symbol = %(symbol)s
          AND toDate(ts) = toDate(%(date)s)
          AND (ema_9 IS NOT NULL OR ema_21 IS NOT NULL OR zscore_close_20 IS NOT NULL)
        """

        try:
            bars_res = self.client.query(query_bars, parameters={'symbol': symbol, 'date': self.date})
            bars_total = bars_res.result_rows[0][0] if bars_res.result_rows else 0
            result["bars_total"] = bars_total

            features_res = self.client.query(query_features, parameters={'symbol': symbol, 'date': self.date})
            features_available = features_res.result_rows[0][0] if features_res.result_rows else 0
            result["features_available"] = features_available

            if bars_total > 0:
                result["coverage_pct"] = round((features_available / bars_total) * 100, 2)

            if result["coverage_pct"] >= 99:
                result["status"] = "pass"
                result["message"] = f"Complete: {features_available}/{bars_total} bars have features"
                self.log("info", check_name, f"✅ {symbol}: {result['message']}")
            elif result["coverage_pct"] >= 90:
                result["status"] = "warning"
                result["message"] = f"Mostly complete: {result['coverage_pct']}% coverage"
                self.log("warning", check_name, f"⚠️ {symbol}: {result['message']}")
            else:
                result["status"] = "error"
                result["message"] = f"Incomplete: {result['coverage_pct']}% coverage"
                self.log("error", check_name, f"❌ {symbol}: {result['message']}")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Query failed: {str(e)}"
            self.log("error", check_name, f"❌ {symbol}: {result['message']}")

        return result

    def check_feature_accuracy(self, symbol: str) -> Dict[str, Any]:
        """
        Spot-check feature calculation accuracy
        抽查特征计算准确性
        """
        check_name = "feature_accuracy"
        result = {
            "status": "unknown",
            "samples_checked": 0,
            "accuracy_issues": 0,
            "message": ""
        }

        # Sample check: EMA should be between recent prices
        # Updated to use current column names: ema_9, ema_21 (not legacy ema_10/ema_20)
        query = """
        SELECT
            ts,
            close,
            ema_9,
            ema_21
        FROM features_1m
        WHERE symbol = %(symbol)s
          AND toDate(ts) = toDate(%(date)s)
          AND ema_9 IS NOT NULL
          AND (ema_9 < close * 0.5 OR ema_9 > close * 1.5
            OR ema_21 < close * 0.5 OR ema_21 > close * 1.5)
        LIMIT 10
        """

        try:
            res = self.client.query(query, parameters={'symbol': symbol, 'date': self.date})
            issues = list(res.result_rows)

            result["samples_checked"] = 10  # Simplified
            result["accuracy_issues"] = len(issues)

            if result["accuracy_issues"] == 0:
                result["status"] = "pass"
                result["message"] = "Feature values look reasonable"
                self.log("info", check_name, f"✅ {symbol}: {result['message']}")
            else:
                result["status"] = "warning"
                result["message"] = f"Found {result['accuracy_issues']} suspicious feature values"
                self.log("warning", check_name, f"⚠️ {symbol}: {result['message']}")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Query failed: {str(e)}"
            self.log("error", check_name, f"❌ {symbol}: {result['message']}")

        return result

    # =========================================================================
    # 4. DATA ACCURACY CHECKS - 数据准确性验证
    # =========================================================================

    def check_ohlc_consistency(self, symbol: str, resolution: str = "1m") -> Dict[str, Any]:
        """
        Check OHLC consistency (High >= Low, High >= Open/Close, etc.)
        检查 OHLC 数据一致性
        """
        check_name = f"ohlc_consistency_{resolution}"
        result = {
            "status": "unknown",
            "inconsistencies": 0,
            "message": ""
        }

        table_map = {
            "1m": "bars_1m_ibkr",
            "5m": "bars_5m_ibkr",
            "15m": "bars_15m_ibkr"
        }
        table = table_map.get(resolution, "bars_1m_ibkr")

        query = f"""
        SELECT count() as cnt
        FROM {table}
        WHERE symbol = %(symbol)s
          AND toDate(ts_start) = toDate(%(date)s)
          AND (
              high < low
              OR high < open
              OR high < close
              OR low > open
              OR low > close
              OR open <= 0
              OR high <= 0
              OR low <= 0
              OR close <= 0
          )
        """

        try:
            res = self.client.query(query, parameters={'symbol': symbol, 'date': self.date})
            inconsistencies = res.result_rows[0][0] if res.result_rows else 0
            result["inconsistencies"] = inconsistencies

            if inconsistencies == 0:
                result["status"] = "pass"
                result["message"] = "All OHLC values consistent"
                self.log("info", check_name, f"✅ {symbol} {resolution}: {result['message']}")
            else:
                result["status"] = "critical"
                result["message"] = f"Found {inconsistencies} inconsistent bars"
                self.log("critical", check_name, f"🔴 {symbol} {resolution}: {result['message']}")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Query failed: {str(e)}"
            self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")

        return result

    def check_price_spikes(self, symbol: str, resolution: str = "1m", threshold: float = 0.10) -> Dict[str, Any]:
        """
        Check for unusual price spikes (>10% move in one bar)
        检查异常价格波动
        """
        check_name = f"price_spikes_{resolution}"
        result = {
            "status": "unknown",
            "spikes_found": 0,
            "max_spike_pct": 0.0,
            "spikes": [],
            "message": ""
        }

        table_map = {
            "1m": "bars_1m_ibkr",
            "5m": "bars_5m_ibkr"
        }
        table = table_map.get(resolution, "bars_1m_ibkr")

        query = f"""
        WITH bar_changes AS (
            SELECT
                ts_start,
                close,
                lagInFrame(close) OVER (ORDER BY ts_start) AS prev_close,
                if(prev_close > 0,
                   abs((close - prev_close) / prev_close),
                   0) AS change_pct_raw
            FROM {table}
            WHERE symbol = %(symbol)s
              AND toDate(ts_start) = toDate(%(date)s)
            ORDER BY ts_start
        )
        SELECT
            ts_start,
            prev_close,
            close,
            round(change_pct_raw * 100, 2) AS change_pct
        FROM bar_changes
        WHERE change_pct_raw > %(threshold)s
          AND prev_close > 0
        ORDER BY change_pct_raw DESC
        LIMIT 10
        """

        try:
            res = self.client.query(query, parameters={
                'symbol': symbol,
                'date': self.date,
                'threshold': threshold
            })

            spikes = []
            for row in res.result_rows:
                ts, prev_close, close, change_pct = row
                spikes.append({
                    "timestamp": str(ts),
                    "prev_close": prev_close,
                    "close": close,
                    "change_pct": change_pct
                })

            result["spikes_found"] = len(spikes)
            result["spikes"] = spikes[:5]
            result["max_spike_pct"] = spikes[0]["change_pct"] if spikes else 0.0

            if result["spikes_found"] == 0:
                result["status"] = "pass"
                result["message"] = f"No unusual spikes (>{threshold*100}%)"
                self.log("info", check_name, f"✅ {symbol} {resolution}: {result['message']}")
            elif result["spikes_found"] <= 2:
                result["status"] = "warning"
                result["message"] = f"Found {result['spikes_found']} potential spikes (max: {result['max_spike_pct']}%)"
                self.log("warning", check_name, f"⚠️ {symbol} {resolution}: {result['message']}")
            else:
                result["status"] = "error"
                result["message"] = f"Found {result['spikes_found']} price spikes (max: {result['max_spike_pct']}%)"
                self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Query failed: {str(e)}"
            self.log("error", check_name, f"❌ {symbol} {resolution}: {result['message']}")

        return result

    # =========================================================================
    # 5. PERFORMANCE & INFRASTRUCTURE CHECKS
    # =========================================================================

    def check_kafka_consumer_lag(self) -> Dict[str, Any]:
        """
        Check Kafka consumer group lag
        检查 Kafka 消费者延迟
        """
        check_name = "kafka_consumer_lag"
        result = {
            "status": "unknown",
            "high_lag_groups": [],
            "total_groups_checked": 0,
            "message": ""
        }

        try:
            import subprocess
            # Use docker exec to run kafka command inside container
            cmd = "docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --all-groups --describe"
            output = subprocess.check_output(cmd, shell=True, text=True, timeout=10)

            # Parse output for LAG column
            high_lag = []
            for line in output.split('\n'):
                if 'LAG' in line and not line.startswith('GROUP'):
                    parts = line.split()
                    if len(parts) >= 6:
                        try:
                            lag = int(parts[5])
                            if lag > 1000:
                                high_lag.append({
                                    "group": parts[0],
                                    "topic": parts[1],
                                    "lag": lag
                                })
                        except (ValueError, IndexError):
                            pass

            result["high_lag_groups"] = high_lag
            result["total_groups_checked"] = len(output.split('\n'))

            if len(high_lag) == 0:
                result["status"] = "pass"
                result["message"] = "All consumer groups healthy"
                self.log("info", check_name, result["message"])
            else:
                result["status"] = "warning"
                result["message"] = f"Found {len(high_lag)} groups with high lag"
                self.log("warning", check_name, result["message"])

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Check failed: {str(e)}"
            self.log("error", check_name, result["message"])

        return result

    # =========================================================================
    # MAIN VALIDATION RUNNER
    # =========================================================================

    def run_all_checks(self, check_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """Run all validation checks"""
        if check_types is None:
            check_types = ["completeness", "continuity", "features", "accuracy"]

        for symbol in self.symbols:
            self.results["checks"][symbol] = {}

            # 1. Completeness checks
            if "completeness" in check_types:
                self.results["checks"][symbol]["bar_completeness_1m"] = self.check_bar_completeness(symbol, "1m")
                self.results["checks"][symbol]["bar_completeness_5m"] = self.check_bar_completeness(symbol, "5m")
                self.results["checks"][symbol]["tick_volume"] = self.check_tick_data_volume(symbol)

            # 2. Continuity checks
            if "continuity" in check_types:
                self.results["checks"][symbol]["time_gaps_1m"] = self.check_time_gaps(symbol, "1m")
                self.results["checks"][symbol]["duplicate_bars_1m"] = self.check_duplicate_bars(symbol, "1m")

            # 3. Feature checks
            if "features" in check_types:
                self.results["checks"][symbol]["feature_completeness"] = self.check_feature_completeness(symbol)
                self.results["checks"][symbol]["feature_accuracy"] = self.check_feature_accuracy(symbol)

            # 4. Accuracy checks
            if "accuracy" in check_types:
                self.results["checks"][symbol]["ohlc_consistency_1m"] = self.check_ohlc_consistency(symbol, "1m")
                self.results["checks"][symbol]["price_spikes_1m"] = self.check_price_spikes(symbol, "1m")

            # Count checks
            for check_result in self.results["checks"][symbol].values():
                self.results["summary"]["total_checks"] += 1
                if check_result["status"] == "pass":
                    self.results["summary"]["passed"] += 1

        # 5. Infrastructure checks (once for all symbols)
        if "infrastructure" in check_types:
            self.results["checks"]["_infrastructure"] = {
                "kafka_consumer_lag": self.check_kafka_consumer_lag()
            }

        return self.results

    def print_summary(self):
        """Print validation summary"""
        print("\n" + "=" * 80)
        print("📊 DATA VALIDATION SUMMARY")
        print("=" * 80)
        print(f"Date: {self.date}")
        print(f"Symbols: {', '.join(self.symbols)}")
        print(f"\nResults:")
        print(f"  Total Checks:     {self.results['summary']['total_checks']}")
        print(f"  ✅ Passed:        {self.results['summary']['passed']}")
        print(f"  ⚠️  Warnings:      {self.results['summary']['warnings']}")
        print(f"  ❌ Errors:        {self.results['summary']['errors']}")
        print(f"  🔴 Critical:      {self.results['summary']['critical_errors']}")

        success_rate = (self.results['summary']['passed'] / self.results['summary']['total_checks'] * 100) \
            if self.results['summary']['total_checks'] > 0 else 0

        print(f"\n  Success Rate: {success_rate:.1f}%")
        print("=" * 80)

        if self.results['summary']['critical_errors'] > 0:
            print("\n🔴 CRITICAL ISSUES FOUND - Immediate action required!")
            return False
        elif self.results['summary']['errors'] > 0:
            print("\n⚠️  ERRORS FOUND - Review recommended")
            return False
        else:
            print("\n✅ All checks passed successfully")
            return True

    def save_report(self, output_file: str):
        """Save validation report to JSON file"""
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w') as f:
            json.dump(self.results, f, indent=2, default=str)

        logger.info(f"Report saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive Data Quality Validation Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full daily validation
  python tools/comprehensive_data_validation.py --date 2025-10-27 --symbols AAPL,TSLA,NVDA --full

  # Quick check
  python tools/comprehensive_data_validation.py --symbols AAPL,TSLA --quick

  # Specific checks
  python tools/comprehensive_data_validation.py --symbols AAPL --checks continuity,features

  # With JSON output
  python tools/comprehensive_data_validation.py --symbols AAPL --output reports/data_quality.json
        """
    )

    parser.add_argument(
        "--date",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Date to validate (YYYY-MM-DD). Defaults to today."
    )

    parser.add_argument(
        "--symbols",
        type=str,
        required=True,
        help="Comma-separated list of symbols (e.g., 'AAPL,TSLA,NVDA')"
    )

    parser.add_argument(
        "--checks",
        type=str,
        default="completeness,continuity,features,accuracy",
        help="Comma-separated checks: completeness,continuity,features,accuracy,infrastructure"
    )

    parser.add_argument(
        "--full",
        action="store_true",
        help="Run all checks (same as --checks all)"
    )

    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run only critical checks (completeness, accuracy)"
    )

    parser.add_argument(
        "--output",
        type=str,
        help="Output JSON report path (e.g., reports/data_quality.json)"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()

    # Parse symbols
    symbols = [s.strip().upper() for s in args.symbols.split(',')]

    # Parse check types
    if args.full:
        check_types = ["completeness", "continuity", "features", "accuracy", "infrastructure"]
    elif args.quick:
        check_types = ["completeness", "accuracy"]
    else:
        check_types = [c.strip().lower() for c in args.checks.split(',')]

    # Create validator
    validator = DataValidator(symbols, args.date, args.verbose)

    # Connect to ClickHouse
    if not validator.connect():
        sys.exit(1)

    # Run validation
    print(f"\n🔍 Starting comprehensive data validation...")
    print(f"Date: {args.date}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Checks: {', '.join(check_types)}")

    validator.run_all_checks(check_types)

    # Print summary
    success = validator.print_summary()

    # Save report if requested
    if args.output:
        validator.save_report(args.output)

    # Exit code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
