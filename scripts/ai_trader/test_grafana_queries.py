#!/usr/bin/env python3
"""
Test Grafana Dashboard Queries

验证所有 Grafana Dashboard 查询在 ClickHouse 中正常工作
"""
from libs.db.clickhouse import ClickHouseDB
from datetime import date, datetime

def test_query(ch, name, query, expected_min_rows=0):
    """测试单个查询"""
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"{'='*60}")
    print(f"Query: {query[:100]}...")

    try:
        result = ch.query(query)
        row_count = len(result) if isinstance(result, list) else 1

        if row_count >= expected_min_rows:
            print(f"✅ SUCCESS: {row_count} rows returned")
            if row_count > 0 and row_count <= 3:
                print(f"Sample data: {result}")
        else:
            print(f"⚠️  WARNING: Only {row_count} rows (expected >= {expected_min_rows})")

        return True
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False

def main():
    print("🧪 Grafana Dashboard Query Validation")
    print("=" * 60)

    ch = ClickHouseDB()
    results = {}

    # 1. OOS Sharpe
    results['oos_sharpe'] = test_query(
        ch,
        "OOS Sharpe (Today)",
        "SELECT anyLast(value) FROM ai_trader_dash_metrics WHERE date=today() AND session='learn' AND name='oos_sharpe'"
    )

    # 2. DQ Fail Count
    results['dq_fail'] = test_query(
        ch,
        "DQ Fail Count (Today)",
        "SELECT anyLast(value) FROM ai_trader_dash_metrics WHERE date=today() AND session='learn' AND name='dq_failed_count'"
    )

    # 3. OOS Sharpe 7d
    results['oos_7d'] = test_query(
        ch,
        "OOS Sharpe (7 days)",
        "SELECT ts, value FROM ai_trader_dash_metrics WHERE name='oos_sharpe' AND ts > now() - INTERVAL 7 DAY ORDER BY ts"
    )

    # 4. Guardian Events
    results['guardian'] = test_query(
        ch,
        "Guardian Events",
        "SELECT ts, symbol, JSONExtractString(payload, 'action') AS action, JSONExtractString(payload, 'reason') AS reason FROM ai_trader_events WHERE kind='guardian' AND ts>now()-INTERVAL 2 DAY ORDER BY ts DESC LIMIT 10"
    )

    # 5. Tool Watchdog
    results['tool'] = test_query(
        ch,
        "Tool Watchdog",
        "SELECT ts, JSONExtractString(payload, 'event') AS event, JSONExtractString(payload, 'reason') AS reason FROM ai_trader_events WHERE kind='tool' AND ts>now()-INTERVAL 2 DAY ORDER BY ts DESC LIMIT 10"
    )

    # 6. Hedging Events
    results['hedging'] = test_query(
        ch,
        "Hedging Events",
        "SELECT ts, symbol, JSONExtractString(payload, 'hedge_symbol') AS hedge_sym FROM ai_trader_events WHERE kind='hedging' AND date=today() ORDER BY ts DESC LIMIT 10"
    )

    # 7. Brain Calls (Most Important!)
    results['brain_calls'] = test_query(
        ch,
        "Brain Calls (Recent)",
        """SELECT
            ts,
            JSONExtractString(payload, 'kind') AS kind,
            JSONExtractString(payload, 'session') AS session,
            JSONExtractString(payload, 'action') AS action,
            JSONExtractString(payload, 'provider') AS provider,
            left(JSONExtractString(payload, 'output'), 200) AS output_snip
        FROM ai_trader_events
        WHERE kind='brain'
        AND ts>now()-INTERVAL 2 DAY
        ORDER BY ts DESC
        LIMIT 5""",
        expected_min_rows=1
    )

    # 8. Brain Calls Count Today
    results['brain_count'] = test_query(
        ch,
        "Brain Calls Count (Today)",
        "SELECT count() AS count FROM ai_trader_events WHERE kind='brain' AND toDate(ts) = today()",
        expected_min_rows=1
    )

    # 9. Brain Success Rate
    results['brain_success'] = test_query(
        ch,
        "Brain Success Rate (Today)",
        """SELECT
            round(countIf(NOT JSONHas(payload, 'output.error')) * 100.0 / count(), 2) AS success_rate
        FROM ai_trader_events
        WHERE kind='brain'
        AND toDate(ts) = today()"""
    )

    # Summary
    print(f"\n{'='*60}")
    print("📊 Summary")
    print(f"{'='*60}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)

    for name, success in results.items():
        status = "✅" if success else "❌"
        print(f"{status} {name}")

    print(f"\n{'='*60}")
    print(f"Total: {passed}/{total} queries passed")

    if passed == total:
        print("✅ All queries working! Dashboard should work fine.")
    else:
        print(f"⚠️  {total - passed} queries failed. Check errors above.")

    return passed == total

if __name__ == "__main__":
    import sys
    success = main()
    sys.exit(0 if success else 1)
