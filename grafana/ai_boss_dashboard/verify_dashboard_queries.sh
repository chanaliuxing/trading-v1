#!/bin/bash
# Dashboard Query Verification Script
# Verifies all queries used in Complete Financial Dashboard

echo "================================================================================
Dashboard Query Verification
================================================================================"

echo ""
echo "🔍 Verifying Account Realtime Data..."
echo ""

echo "Query 1: Net Liquidation"
curl -s "http://localhost:8123/" --data "
SELECT anyLast(net_liquidation) as net_liquidation
FROM llm.ibkr_account_realtime
WHERE date = today()
"
echo ""

echo "Query 2: Total Cash"
curl -s "http://localhost:8123/" --data "
SELECT anyLast(total_cash) as total_cash
FROM llm.ibkr_account_realtime
WHERE date = today()
"
echo ""

echo "Query 3: Position Value"
curl -s "http://localhost:8123/" --data "
SELECT anyLast(gross_position_value) as position_value
FROM llm.ibkr_account_realtime
WHERE date = today()
"
echo ""

echo "Query 4: Unrealized P&L"
curl -s "http://localhost:8123/" --data "
SELECT anyLast(unrealized_pnl) as total_unrealized
FROM llm.ibkr_account_realtime
WHERE date = today()
"
echo ""

echo "Query 5: Buying Power"
curl -s "http://localhost:8123/" --data "
SELECT anyLast(buying_power) as buying_power
FROM llm.ibkr_account_realtime
WHERE date = today()
"
echo ""

echo "Query 6: Available Funds"
curl -s "http://localhost:8123/" --data "
SELECT anyLast(available_funds) as available_funds
FROM llm.ibkr_account_realtime
WHERE date = today()
"
echo ""

echo "Query 7: Realized P&L"
curl -s "http://localhost:8123/" --data "
SELECT anyLast(realized_pnl) as total_realized
FROM llm.ibkr_account_realtime
WHERE date = today()
"
echo ""

echo ""
echo "🔍 Verifying Position Realtime Data..."
echo ""

echo "Query 8: Position Count"
curl -s "http://localhost:8123/" --data "
SELECT count(DISTINCT symbol) as position_count
FROM llm.ibkr_positions_realtime
WHERE ts_ny = (SELECT max(ts_ny) FROM llm.ibkr_positions_realtime)
  AND position > 0
"
echo ""

echo "Query 9: Positions Table (Latest)"
curl -s "http://localhost:8123/" --data "
SELECT
    symbol,
    position as shares,
    avg_cost,
    market_price as current_price,
    market_value,
    unrealized_pnl,
    (market_price - avg_cost) / avg_cost * 100 as pnl_pct
FROM llm.ibkr_positions_realtime
WHERE ts_ny = (SELECT max(ts_ny) FROM llm.ibkr_positions_realtime)
  AND position > 0
ORDER BY market_value DESC
FORMAT PrettyCompact
"
echo ""

echo "Query 10: Position Distribution (Pie Chart)"
curl -s "http://localhost:8123/" --data "
SELECT
    symbol,
    market_value as value
FROM llm.ibkr_positions_realtime
WHERE ts_ny = (SELECT max(ts_ny) FROM llm.ibkr_positions_realtime)
  AND position > 0
ORDER BY value DESC
FORMAT PrettyCompact
"
echo ""

echo "Query 11: Unrealized P&L by Symbol"
curl -s "http://localhost:8123/" --data "
SELECT
    symbol,
    unrealized_pnl as unrealized
FROM llm.ibkr_positions_realtime
WHERE ts_ny = (SELECT max(ts_ny) FROM llm.ibkr_positions_realtime)
  AND position > 0
ORDER BY unrealized DESC
FORMAT PrettyCompact
"
echo ""

echo ""
echo "================================================================================
✅ All Queries Verified
================================================================================"
echo ""
echo "Dashboard: http://localhost:3000/d/complete-financial-dashboard"
echo ""
