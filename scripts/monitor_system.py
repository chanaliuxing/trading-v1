#!/usr/bin/env python3
"""
System Monitoring Script - Unified Health Snapshot

Monitors data pipeline health using unified SystemHealthSnapshot protocol.
Compatible with AI Brain diagnostic tools and DataHealer.

Phase 1: Refactored to return standard SystemHealthSnapshot structure
"""

import os
import sys

# Ensure project root is on sys.path for local imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from libs.db.ch_pool import get_client
from libs.logger import get_logger
from libs.health import (
    SystemHealthSnapshot,
    build_system_health,
    BarHealthStatus,
    FeatureHealthStatus,
    LabelHealthStatus,
)

logger = get_logger(__name__)


class SystemMonitor:
    """Comprehensive system health monitor"""

    def __init__(self):
        self.ch = get_client()  # Use global connection pool

    def check_tick_data_health(self) -> dict:
        """Monitor tick data lake health"""
        query = """
        SELECT
            COUNT(*) as total_ticks,
            COUNT(DISTINCT symbol) as unique_symbols,
            MIN(ts) as earliest,
            MAX(ts) as latest,
            dateDiff('hour', MIN(ts), MAX(ts)) as hours_coverage,
            COUNT(*) / greatest(1, dateDiff('hour', MIN(ts), MAX(ts))) as ticks_per_hour
        FROM lake.ticks_trades
        WHERE ts > now() - INTERVAL 7 DAY
        """
        result = self.ch.query(query)
        return result[0] if result else {}

    def check_quote_data_health(self) -> dict:
        """Monitor quote data lake health"""
        query = """
        SELECT
            COUNT(*) as total_quotes,
            COUNT(DISTINCT symbol) as unique_symbols,
            AVG((ask - bid) / ((bid + ask) / 2) * 10000) as avg_spread_bps,
            quantile(0.95)((ask - bid) / ((bid + ask) / 2) * 10000) as p95_spread_bps
        FROM lake.quotes_l1
        WHERE ts > now() - INTERVAL 1 DAY
          AND bid > 0 AND ask > 0
        """
        result = self.ch.query(query)
        return result[0] if result else {}

    def check_option_ml_status(self) -> dict:
        """Check status of Option ML models"""
        query = """
        SELECT updated_at, active_model_version, enabled
        FROM trading.option_ml_controls
        ORDER BY updated_at DESC LIMIT 1
        """
        try:
            res = self.ch.query(query)
            if not res:
                return {"status": "missing", "age_hours": 999.0, "active_model": None}
            
            row = res[0]
            age = (datetime.now() - row["updated_at"]).total_seconds() / 3600
            return {
                "status": "active" if row["enabled"] else "disabled",
                "active_model": row["active_model_version"],
                "age_hours": round(age, 1),
                "needs_retrain": age > 24
            }
        except Exception:
            return {"status": "error", "error": "check_failed"}

    def check_bar_freshness(self) -> dict:
        """Check how fresh bars are (detect staleness)"""
        query = """
        SELECT
            '5s' as resolution,
            MAX(ts_end) as latest_bar,
            dateDiff('second', MAX(ts_end), now()) as seconds_old
        FROM trading.bars_5s
        UNION ALL
        SELECT
            '1m' as resolution,
            MAX(ts_end) as latest_bar,
            dateDiff('second', MAX(ts_end), now()) as seconds_old
        FROM trading.bars_1m_ibkr
        UNION ALL
        SELECT
            '5m' as resolution,
            MAX(ts_end) as latest_bar,
            dateDiff('second', MAX(ts_end), now()) as seconds_old
        FROM trading.bars_5m_ibkr
        """
        result = self.ch.query(query)
        return {row['resolution']: row for row in result}

    def check_gap_rate(self, hours=24) -> dict:
        """Check gap rate per symbol/resolution"""
        query = f"""
        WITH expected_bars AS (
            SELECT
                symbol,
                toStartOfFiveMinutes(ts_start) as bar_time
            FROM trading.bars_5s
            WHERE ts_start > now() - INTERVAL {hours} HOUR
              AND toHour(toTimeZone(ts_start, 'America/New_York')) * 60
                  + toMinute(toTimeZone(ts_start, 'America/New_York')) BETWEEN 570 AND 959
            GROUP BY symbol, bar_time
        ),
        actual_bars AS (
            SELECT
                symbol,
                toStartOfFiveMinutes(ts_start) as bar_time,
                COUNT(*) as bar_count
            FROM trading.bars_5s
            WHERE ts_start > now() - INTERVAL {hours} HOUR
            GROUP BY symbol, bar_time
        )
        SELECT
            e.symbol,
            COUNT(*) as expected_count,
            SUM(CASE WHEN a.bar_count IS NULL THEN 1 ELSE 0 END) as missing_count,
            SUM(CASE WHEN a.bar_count IS NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as gap_rate_pct
        FROM expected_bars e
        LEFT JOIN actual_bars a ON e.symbol = a.symbol AND e.bar_time = a.bar_time
        GROUP BY e.symbol
        ORDER BY gap_rate_pct DESC
        """
        result = self.ch.query(query)
        return result

    def check_vix_status(self) -> dict:
        """Check VIX data availability"""
        query = """
        SELECT
            COUNT(*) as vix_ticks,
            MAX(ts) as latest_vix,
            dateDiff('minute', MAX(ts), now()) as minutes_old
        FROM trading.refs_vix_ticks
        WHERE ts > now() - INTERVAL 1 DAY
        """
        result = self.ch.query(query)
        return result[0] if result else {}

    def check_feature_freshness(self) -> dict:
        """Check feature table freshness"""
        query = """
        SELECT
            '5s' as resolution,
            COUNT(*) as feature_count,
            COUNT(DISTINCT symbol) as symbols,
            MAX(ts) as latest_feature,
            dateDiff('second', MAX(ts), now()) as seconds_old
        FROM trading.features_5s
        WHERE ts > now() - INTERVAL 1 HOUR
        UNION ALL
        SELECT
            '1m' as resolution,
            COUNT(*) as feature_count,
            COUNT(DISTINCT symbol) as symbols,
            MAX(ts) as latest_feature,
            dateDiff('second', MAX(ts), now()) as seconds_old
        FROM trading.features_1m
        WHERE ts > now() - INTERVAL 1 HOUR
        """
        result = self.ch.query(query)
        return {row['resolution']: row for row in result}

    def check_label_health(self, symbols=None, horizons=[30]) -> dict:
        """
        Check label health for ML training

        Args:
            symbols: List of symbols to check (if None, checks all symbols in table)
            horizons: List of label horizons to check (e.g., [30] for 30-minute forward returns)

        Returns:
            {
                "by_symbol": {
                    "TSLA": {
                        "rows": 159545,
                        "null_ratio": 0.0,
                        "age_hours": 2.5,
                        "status": "healthy"
                    },
                    ...
                },
                "summary": {
                    "total_rows": 1315213,
                    "missing_symbols": [],
                    "healthy_count": 15,
                    "overall_status": "healthy"
                }
            }
        """
        # Build label column names
        label_cols = [f"label_h{h}" for h in horizons]

        # Check if table exists
        try:
            table_exists = self.ch.query("""
                SELECT count() as cnt
                FROM system.tables
                WHERE database='trading' AND name='features_1m_with_labels'
            """)

            if not table_exists or table_exists[0]['cnt'] == 0:
                return {
                    "by_symbol": {},
                    "summary": {
                        "total_rows": 0,
                        "missing_symbols": symbols or [],
                        "healthy_count": 0,
                        "overall_status": "table_missing"
                    }
                }
        except Exception as e:
            logger.error(f"Failed to check labels table: {e}")
            return {
                "by_symbol": {},
                "summary": {
                    "total_rows": 0,
                    "error": str(e),
                    "overall_status": "error"
                }
            }

        # Get symbol list if not provided
        if symbols is None:
            try:
                symbol_query = """
                    SELECT DISTINCT symbol
                    FROM trading.features_1m_with_labels
                    ORDER BY symbol
                """
                symbol_result = self.ch.query(symbol_query)
                symbols = [row['symbol'] for row in symbol_result]
            except Exception as e:
                logger.error(f"Failed to get symbols: {e}")
                symbols = []

        if not symbols:
            return {
                "by_symbol": {},
                "summary": {
                    "total_rows": 0,
                    "missing_symbols": [],
                    "healthy_count": 0,
                    "overall_status": "no_data"
                }
            }

        by_symbol = {}
        total_rows = 0
        healthy_count = 0
        low_coverage = []
        high_null = []
        stale = []

        for symbol in symbols:
            try:
                stats_query = f"""
                    SELECT
                        count() as total_rows,
                        max(ts) as latest_ts,
                        countIf({label_cols[0]} IS NULL) as null_count,
                        dateDiff('hour', max(ts), now()) as age_hours
                    FROM trading.features_1m_with_labels
                    WHERE symbol = %(symbol)s
                """

                result = self.ch.query(stats_query, {"symbol": symbol})

                if not result or result[0]['total_rows'] == 0:
                    by_symbol[symbol] = {
                        "status": "missing",
                        "rows": 0,
                        "null_ratio": 1.0,
                        "age_hours": None
                    }
                    continue

                row = result[0]
                rows = row['total_rows']
                null_count = row['null_count']
                null_ratio = null_count / rows if rows > 0 else 1.0
                age_hours = row['age_hours']

                # Determine status
                status = "healthy"
                if rows < 1000:
                    status = "low_coverage"
                    low_coverage.append(symbol)
                elif null_ratio > 0.1:
                    status = "high_nulls"
                    high_null.append(symbol)
                elif age_hours and age_hours > 48:
                    status = "stale"
                    stale.append(symbol)
                else:
                    healthy_count += 1

                by_symbol[symbol] = {
                    "status": status,
                    "rows": rows,
                    "null_ratio": round(null_ratio, 4),
                    "age_hours": round(age_hours, 2) if age_hours is not None else None,
                    "latest_ts": str(row['latest_ts']) if row['latest_ts'] else None
                }
                total_rows += rows

            except Exception as e:
                logger.error(f"Failed to check labels for {symbol}: {e}")
                by_symbol[symbol] = {
                    "status": "error",
                    "error": str(e)
                }

        # Determine overall status
        missing_symbols = [s for s in symbols if by_symbol.get(s, {}).get('status') == 'missing']

        if healthy_count == len(symbols):
            overall_status = "healthy"
        elif missing_symbols or low_coverage or high_null:
            overall_status = "unhealthy"
        elif stale:
            overall_status = "stale"
        else:
            overall_status = "degraded"

        return {
            "by_symbol": by_symbol,
            "summary": {
                "total_rows": total_rows,
                "missing_symbols": missing_symbols,
                "low_coverage_symbols": low_coverage,
                "high_null_symbols": high_null,
                "stale_symbols": stale,
                "healthy_count": healthy_count,
                "overall_status": overall_status
            }
        }

    def check_backfill_queue(self) -> dict:
        """Check backfill queue status (would need Redis or Kafka monitoring)"""
        # This is a placeholder - in production you'd monitor Kafka lag
        return {
            "note": "Monitor Kafka consumer lag for backfill groups",
            "groups_to_monitor": [
                "backfill.5s.v1",
                "backfill.1m.v1",
                "backfill.5m.v1",
                "backfill.15m.v1"
            ]
        }

    def generate_health_snapshot(
        self,
        symbols: List[str] = None,
        horizons: List[int] = [120],
        table: str = "trading.features_1m_with_labels"
    ) -> SystemHealthSnapshot:
        """
        Generate unified system health snapshot (NEW)

        This is the new unified interface that returns SystemHealthSnapshot.
        Use this for integration with AI Brain and DataHealer.

        Args:
            symbols: List of symbols to check (defaults to all symbols in table)
            horizons: Label horizons to check
            table: Feature table to check

        Returns:
            SystemHealthSnapshot with complete system health status
        """
        # If symbols not provided, get from database
        if symbols is None:
            try:
                symbol_query = """
                    SELECT DISTINCT symbol
                    FROM trading.features_1m_with_labels
                    ORDER BY symbol
                    LIMIT 20
                """
                symbol_result = self.ch.query(symbol_query)
                symbols = [row['symbol'] for row in symbol_result]
            except Exception as e:
                logger.error(f"Failed to get symbols: {e}")
                symbols = ["TSLA"]  # Fallback

        # Use unified health snapshot builder
        snapshot = build_system_health(
            symbols=symbols,
            horizons=horizons,
            table=table
        )

        return snapshot

    def generate_health_report(self) -> dict:
        """
        Generate comprehensive health report (LEGACY)

        This is the old interface, kept for backward compatibility.
        New code should use generate_health_snapshot() instead.
        """
        report = {
            "timestamp": datetime.now().isoformat(),
            "tick_data": self.check_tick_data_health(),
            "quote_data": self.check_quote_data_health(),
            "bar_freshness": self.check_bar_freshness(),
            "gap_rate": self.check_gap_rate(hours=24),
            "vix_status": self.check_vix_status(),
            "feature_freshness": self.check_feature_freshness(),
            "label_health": self.check_label_health(),
            "option_ml_status": self.check_option_ml_status(),
            "backfill_status": self.check_backfill_queue()
        }
        return report

    def print_health_report(self):
        """Print human-readable health report"""
        report = self.generate_health_report()

        print("=" * 80)
        print("TRADING SYSTEM HEALTH REPORT")
        print("=" * 80)
        print(f"Generated: {report['timestamp']}")
        print()

        # Tick Data
        print("📊 TICK DATA LAKE")
        print("-" * 80)
        tick = report['tick_data']
        print(f"  Total ticks (7d):     {tick.get('total_ticks', 0):,}")
        print(f"  Unique symbols:       {tick.get('unique_symbols', 0)}")
        print(f"  Coverage (hours):     {tick.get('hours_coverage', 0)}")
        print(f"  Ticks/hour:           {tick.get('ticks_per_hour', 0):.0f}")
        print()

        # Quote Data
        print("💰 QUOTE DATA & SPREADS")
        print("-" * 80)
        quote = report['quote_data']
        print(f"  Total quotes (24h):   {quote.get('total_quotes', 0):,}")
        print(f"  Avg spread (bps):     {quote.get('avg_spread_bps', 0):.2f}")
        print(f"  P95 spread (bps):     {quote.get('p95_spread_bps', 0):.2f}")
        print()

        # Bar Freshness
        print("📈 BAR FRESHNESS")
        print("-" * 80)
        for res, data in report['bar_freshness'].items():
            age_sec = data.get('seconds_old', 999999)
            status = "✅" if age_sec < 300 else "⚠️" if age_sec < 3600 else "❌"
            print(f"  {res:5} {status} Latest: {data.get('latest_bar', 'N/A')} (age: {age_sec}s)")
        print()

        # Gap Rate
        print("🕳️  GAP RATE (24h)")
        print("-" * 80)
        gaps = report['gap_rate']
        if gaps:
            for row in gaps[:5]:  # Top 5 worst
                symbol = row['symbol']
                gap_pct = row['gap_rate_pct']
                status = "✅" if gap_pct < 1 else "⚠️" if gap_pct < 5 else "❌"
                print(f"  {status} {symbol:6} Gap rate: {gap_pct:5.2f}% ({row['missing_count']}/{row['expected_count']})")
        else:
            print("  No gap data available")
        print()

        # VIX Status
        print("📉 VIX DATA")
        print("-" * 80)
        vix = report['vix_status']
        vix_count = vix.get('vix_ticks', 0)
        vix_age = vix.get('minutes_old', 999999)
        vix_status = "✅" if vix_count > 0 and vix_age < 60 else "⚠️" if vix_count > 0 else "❌"
        print(f"  {vix_status} VIX ticks (24h):    {vix_count}")
        print(f"  {vix_status} Latest VIX:         {vix.get('latest_vix', 'N/A')} (age: {vix_age}m)")
        print()

        # Feature Freshness
        print("🧮 FEATURE FRESHNESS")
        print("-" * 80)
        features = report['feature_freshness']
        for res, data in features.items():
            count = data.get('feature_count', 0)
            age_sec = data.get('seconds_old', 999999)
            status = "✅" if count > 0 and age_sec < 300 else "⚠️" if count > 0 else "❌"
            print(f"  {res:5} {status} Count: {count:6,} | Symbols: {data.get('symbols', 0)} | Age: {age_sec}s")
        print()

        # Label Health (NEW)
        print("🏷️  LABEL HEALTH (ML Training Readiness)")
        print("-" * 80)
        labels = report.get('label_health', {})
        summary = labels.get('summary', {})
        overall = summary.get('overall_status', 'unknown')

        if overall == 'healthy':
            status_icon = "✅"
        elif overall in ['stale', 'degraded']:
            status_icon = "⚠️"
        else:
            status_icon = "❌"

        print(f"  {status_icon} Overall Status: {overall.upper()}")
        print(f"  Total Rows:      {summary.get('total_rows', 0):,}")
        print(f"  Healthy Symbols: {summary.get('healthy_count', 0)}")

        if summary.get('missing_symbols'):
            print(f"  ❌ Missing:       {', '.join(summary['missing_symbols'])}")
        if summary.get('low_coverage_symbols'):
            print(f"  ⚠️  Low Coverage:  {', '.join(summary['low_coverage_symbols'])}")
        if summary.get('high_null_symbols'):
            print(f"  ⚠️  High Nulls:    {', '.join(summary['high_null_symbols'])}")
        if summary.get('stale_symbols'):
            print(f"  ⚠️  Stale:         {', '.join(summary['stale_symbols'])}")

        # Show top 5 symbols with most labels
        by_symbol = labels.get('by_symbol', {})
        if by_symbol:
            top_symbols = sorted(by_symbol.items(), key=lambda x: x[1].get('rows', 0), reverse=True)[:5]
            print(f"\n  Top 5 Symbols by Coverage:")
            for sym, data in top_symbols:
                sym_status = "✅" if data.get('status') == 'healthy' else "⚠️" if data.get('status') in ['stale', 'low_coverage'] else "❌"
                print(f"    {sym_status} {sym:6} {data.get('rows', 0):7,} rows | null: {data.get('null_ratio', 0):.2%} | age: {data.get('age_hours', 0):.1f}h")
        print()

        print("=" * 80)
        print("For Kafka monitoring, check consumer lag for backfill groups")
        print("=" * 80)
        print()

    def print_health_snapshot(
        self,
        symbols: List[str] = None,
        horizons: List[int] = [120]
    ):
        """
        Print unified health snapshot (NEW)

        This is the new unified printer that uses SystemHealthSnapshot.
        """
        snapshot = self.generate_health_snapshot(symbols=symbols, horizons=horizons)

        print("=" * 80)
        print("UNIFIED SYSTEM HEALTH SNAPSHOT")
        print("=" * 80)
        print(f"Timestamp:        {snapshot.timestamp}")
        print(f"Overall Status:   {snapshot.overall_status.upper()}")
        print(f"Symbols:          {', '.join(snapshot.symbols)}")
        print(f"Horizons:         {', '.join(map(str, snapshot.horizons))}")
        print()

        # Bars Health
        print("📊 BARS HEALTH")
        print("-" * 80)
        bars = snapshot.bars
        status_icon = "✅" if bars.status == "healthy" else "⚠️" if bars.status == "degraded" else "❌"
        print(f"  {status_icon} Status:           {bars.status.upper()}")
        if bars.latest_ts:
            print(f"  Latest:           {bars.latest_ts}")
            print(f"  Age:              {bars.age_hours:.1f} hours")
        print(f"  Coverage:         {bars.coverage_pct:.1%}")
        if bars.missing_symbols:
            print(f"  ❌ Missing:        {', '.join(bars.missing_symbols)}")
        if bars.issues:
            for issue in bars.issues:
                print(f"  ⚠️  Issue:          {issue}")
        print()

        # Features Health
        print("🧮 FEATURES HEALTH")
        print("-" * 80)
        features = snapshot.features
        status_icon = "✅" if features.status == "healthy" else "⚠️" if features.status == "degraded" else "❌"
        print(f"  {status_icon} Status:           {features.status.upper()}")
        if features.latest_ts:
            print(f"  Latest:           {features.latest_ts}")
            print(f"  Age:              {features.age_hours:.1f} hours")
        print(f"  NULL Ratio:       {features.null_ratio:.1%}")
        if features.rebuild_needed:
            print(f"  ⚠️  Rebuild Needed: YES")
        if features.issues:
            for issue in features.issues:
                print(f"  ⚠️  Issue:          {issue}")
        print()

        # Labels Health
        print("🏷️  LABELS HEALTH")
        print("-" * 80)
        labels = snapshot.labels
        status_icon = "✅" if labels.status == "healthy" else "⚠️" if labels.status == "degraded" else "❌"
        print(f"  {status_icon} Overall Status:   {labels.status.upper()}")

        summary = labels.summary
        print(f"  Total Combinations: {summary.get('total_combinations', 0)}")
        print(f"  Healthy:            {summary.get('healthy', 0)}")
        print(f"  Degraded:           {summary.get('degraded', 0)}")
        print(f"  Blocked:            {summary.get('blocked', 0)}")
        print()

        # Show details for each symbol-horizon combination
        if labels.by_symbol:
            print("  Per-Symbol Status:")
            for key, sym_status in labels.by_symbol.items():
                sym_icon = "✅" if sym_status.status == "healthy" else "⚠️" if sym_status.status == "degraded" else "❌"
                print(f"    {sym_icon} {key:15} rows={sym_status.rows:6,} | null={sym_status.null_ratio:5.1%} | "
                      f"std={sym_status.std_bps:6.1f}bps | age={sym_status.age_hours:.1f}h")
                if sym_status.issues:
                    for issue in sym_status.issues:
                        print(f"         Issue: {issue}")
        print()

        # Config Health
        print("⚙️  CONFIG HEALTH")
        print("-" * 80)
        config = snapshot.config
        status_icon = "✅" if config.status == "healthy" else "⚠️" if config.status == "degraded" else "❌"
        print(f"  {status_icon} Status:           {config.status.upper()}")

        if config.problems:
            print(f"  ❌ PROBLEMS:")
            for problem in config.problems:
                print(f"     [{problem.get('severity', 'unknown').upper()}] {problem.get('message', 'Unknown issue')}")

        if config.warnings:
            print(f"  ⚠️  WARNINGS:")
            for warning in config.warnings:
                print(f"     {warning.get('message', 'Unknown warning')}")

        if not config.problems and not config.warnings:
            print(f"  No issues detected")
        print()

        # Blocking Issues
        if snapshot.blocking_issues:
            print("🚨 BLOCKING ISSUES")
            print("-" * 80)
            for i, issue in enumerate(snapshot.blocking_issues, 1):
                print(f"  {i}. {issue}")
            print()

        print("=" * 80)
        print()


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="System Health Monitor")
    parser.add_argument(
        "--mode",
        choices=["legacy", "unified"],
        default="unified",
        help="Report mode: 'legacy' for old format, 'unified' for new SystemHealthSnapshot"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="Symbols to check (defaults to all symbols in DB)"
    )
    parser.add_argument(
        "--horizons",
        type=int,
        nargs="+",
        default=[120],
        help="Label horizons to check (e.g., 30 60 120)"
    )
    parser.add_argument(
        "--output",
        help="Output JSON file path (optional)"
    )

    args = parser.parse_args()

    monitor = SystemMonitor()

    try:
        if args.mode == "unified":
            # New unified mode
            snapshot = monitor.generate_health_snapshot(
                symbols=args.symbols,
                horizons=args.horizons
            )

            # Print to console
            monitor.print_health_snapshot(
                symbols=args.symbols,
                horizons=args.horizons
            )

            # Optionally save to JSON
            if args.output:
                import json
                with open(args.output, 'w') as f:
                    json.dump(snapshot.to_dict(), f, indent=2)
                print(f"✅ Snapshot saved to {args.output}")

        else:
            # Legacy mode
            monitor.print_health_report()

            if args.output:
                import json
                report = monitor.generate_health_report()
                with open(args.output, 'w') as f:
                    json.dump(report, f, indent=2)
                print(f"✅ Report saved to {args.output}")

    except Exception as e:
        logger.error(f"Monitoring failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
