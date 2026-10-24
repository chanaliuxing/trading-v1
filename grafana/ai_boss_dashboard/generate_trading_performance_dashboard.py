#!/usr/bin/env python3
"""
Dashboard 1: Trading Performance Dashboard
100% 可落地版本 - 只使用现有 ClickHouse 数据
"""

import json

ds = {"type": "grafana-clickhouse-datasource", "uid": "ch"}

dashboard = {
    "uid": "ai-trading-performance",
    "title": "AI Trading Performance Dashboard",
    "description": "交易表现 - P&L, Win Rate, Equity Curve, Fills",
    "tags": ["ai-trading", "performance", "pnl"],
    "timezone": "America/New_York",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-7d", "to": "now"},
    "editable": True,

    "templating": {
        "list": [
            {
                "type": "query",
                "name": "symbol",
                "label": "Symbol",
                "datasource": ds,
                "query": "SELECT DISTINCT symbol FROM llm.llm_fills WHERE symbol != '' ORDER BY symbol",
                "refresh": 2,
                "includeAll": True,
                "allValue": "%",
                "current": {"text": "All", "value": "%"}
            }
        ]
    },

    "panels": []
}

panel_id = 1
y = 0

# =============================================================================
# Row 1: 💵 Today's Performance Overview
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "💵 Today's Performance (今日表现)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 1.1: Data Freshness (CRITICAL - Must be first!)
dashboard["panels"].append({
    "type": "stat",
    "title": "⚠️ Data Freshness (数据新鲜度)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 6, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%M:%S ET') as last_update,
    round((now() - ts_ny) / 60, 0) as minutes_ago
FROM llm.ibkr_account_realtime
ORDER BY ts_ny DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 0,
            "mappings": [
                {
                    "type": "value",
                    "options": {
                        "pattern": ".*",
                        "result": {
                            "text": "${__field.name}: ${__value} min ago"
                        }
                    }
                }
            ],
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "green"},      # < 10 min - FRESH
                    {"value": 10, "color": "yellow"},    # 10-60 min - STALE
                    {"value": 60, "color": "orange"},    # 1-6 hours - OLD
                    {"value": 360, "color": "red"}       # > 6 hours - CRITICAL
                ]
            }
        }
    },
    "options": {
        "colorMode": "background",
        "textMode": "value_and_name",
        "orientation": "horizontal"
    }
})
panel_id += 1

# Panel 1.2: Trading Activity Status (CRITICAL - Are we even trading?)
dashboard["panels"].append({
    "type": "stat",
    "title": "📊 Trading Activity (交易活跃度)",
    "datasource": ds,
    "gridPos": {"x": 6, "y": y, "w": 6, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    count(*) as fills_today,
    formatDateTime(max(ts_ny), '%H:%M:%S ET') as last_fill_time,
    round((now() - max(ts_ny)) / 3600, 1) as hours_since_last_fill
FROM llm.llm_fills
WHERE toDate(ts_ny) = today()
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 0,
            "mappings": [],
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "red"},        # 0 fills - WARNING
                    {"value": 1, "color": "yellow"},     # 1-4 fills - LOW
                    {"value": 5, "color": "green"}       # 5+ fills - ACTIVE
                ]
            }
        }
    },
    "options": {
        "colorMode": "background",
        "textMode": "value_and_name",
        "orientation": "horizontal"
    }
})
panel_id += 1

# Panel 1.3: Total P&L (Account Level - Most Accurate)
dashboard["panels"].append({
    "type": "stat",
    "title": "💰 Total P&L (总盈亏)",
    "datasource": ds,
    "gridPos": {"x": 12, "y": y, "w": 3, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    unrealized_pnl + realized_pnl as total_pnl
FROM llm.ibkr_account_realtime
ORDER BY ts_ny DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "decimals": 2,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": -1000, "color": "dark-red"},
                    {"value": -500, "color": "red"},
                    {"value": 0, "color": "yellow"},
                    {"value": 500, "color": "light-green"},
                    {"value": 1000, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "graphMode": "area",
        "colorMode": "background",
        "textMode": "value_and_name",
        "orientation": "horizontal"
    }
})
panel_id += 1
y += 3

# Panel 1.4: Unrealized P&L
dashboard["panels"].append({
    "type": "stat",
    "title": "📈 Unrealized P&L",
    "datasource": ds,
    "gridPos": {"x": 15, "y": y, "w": 3, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    unrealized_pnl
FROM llm.ibkr_account_realtime
ORDER BY ts_ny DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "decimals": 2,
            "color": {"mode": "thresholds"},
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": -500, "color": "red"},
                    {"value": 0, "color": "yellow"},
                    {"value": 500, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "colorMode": "background"
    }
})
panel_id += 1

# Panel 1.5: Realized P&L
dashboard["panels"].append({
    "type": "stat",
    "title": "✅ Realized P&L",
    "datasource": ds,
    "gridPos": {"x": 18, "y": y, "w": 3, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    realized_pnl
FROM llm.ibkr_account_realtime
ORDER BY ts_ny DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "decimals": 2,
            "color": {"mode": "thresholds"},
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": -100, "color": "red"},
                    {"value": 0, "color": "yellow"},
                    {"value": 100, "color": "green"}
                ]
            }
        }
    }
})
panel_id += 1

# Panel 1.6: Net Liquidation (Second row)
dashboard["panels"].append({
    "type": "stat",
    "title": "💵 Net Liquidation (净资产)",
    "datasource": ds,
    "gridPos": {"x": 21, "y": y, "w": 3, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    net_liquidation
FROM llm.ibkr_account_realtime
ORDER BY ts_ny DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "decimals": 2,
            "color": {"mode": "palette-classic"}
        }
    }
})
panel_id += 1
y += 3

# Panel 1.7: Position Count
dashboard["panels"].append({
    "type": "stat",
    "title": "📊 Open Positions (持仓数)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 6, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    count(DISTINCT symbol) as position_count
FROM llm.ibkr_positions_realtime
WHERE ts_ny = (SELECT max(ts_ny) FROM llm.ibkr_positions_realtime)
  AND position > 0
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 0
        }
    }
})
panel_id += 1

# Panel 1.8: Total Fills (7 days)
dashboard["panels"].append({
    "type": "stat",
    "title": "📝 7-Day Fills (7日成交)",
    "datasource": ds,
    "gridPos": {"x": 6, "y": y, "w": 6, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    count(*) as total_fills
FROM llm.llm_fills
WHERE toDate(ts_ny) >= today() - INTERVAL 7 DAY
  AND symbol LIKE '${symbol}'
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 0
        }
    }
})
panel_id += 1

# Panel 1.9: System Status Indicator
dashboard["panels"].append({
    "type": "stat",
    "title": "🚦 System Status (系统状态)",
    "datasource": ds,
    "gridPos": {"x": 12, "y": y, "w": 12, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
WITH data_age AS (
    SELECT round((now() - max(ts_ny)) / 60, 0) as minutes_ago
    FROM llm.ibkr_account_realtime
),
fills_today AS (
    SELECT count(*) as fill_count
    FROM llm.llm_fills
    WHERE toDate(ts_ny) = today()
)
SELECT
    CASE
        WHEN d.minutes_ago > 360 THEN 0  -- CRITICAL: Data > 6h old
        WHEN d.minutes_ago > 60 THEN 1   -- WARNING: Data > 1h old
        WHEN f.fill_count = 0 AND toHour(now()) >= 9 AND toHour(now()) < 16 THEN 1  -- WARNING: No fills during market hours
        ELSE 2  -- OK
    END as status_code,
    multiIf(
        d.minutes_ago > 360, 'CRITICAL: Data stale > 6h',
        d.minutes_ago > 60, 'WARNING: Data stale > 1h',
        f.fill_count = 0 AND toHour(now()) >= 9 AND toHour(now()) < 16, 'WARNING: No fills today',
        'OK: System operational'
    ) as status_text
FROM data_age d, fills_today f
"""
    }],
    "fieldConfig": {
        "defaults": {
            "mappings": [
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "CRITICAL", "color": "red"},
                        "1": {"text": "WARNING", "color": "yellow"},
                        "2": {"text": "OK", "color": "green"}
                    }
                }
            ],
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "red"},
                    {"value": 1, "color": "yellow"},
                    {"value": 2, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "colorMode": "background",
        "textMode": "value_and_name"
    }
})
panel_id += 1
y += 3

# =============================================================================
# Row 2: 📈 Current Positions
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "📈 Current Positions (当前持仓)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 2.1: Positions Table with Estimated P&L
dashboard["panels"].append({
    "type": "table",
    "title": "📋 Current Positions (当前持仓明细)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 16, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
WITH latest_positions AS (
    SELECT
        symbol,
        position as shares,
        avg_cost,
        market_value,
        ts_ny
    FROM llm.ibkr_positions_realtime
    WHERE ts_ny = (SELECT max(ts_ny) FROM llm.ibkr_positions_realtime)
      AND position > 0
),
latest_account AS (
    SELECT
        unrealized_pnl as total_unrealized_pnl,
        gross_position_value as total_position_value
    FROM llm.ibkr_account_realtime
    ORDER BY ts_ny DESC
    LIMIT 1
)
SELECT
    p.symbol,
    p.shares,
    round(p.avg_cost, 2) as avg_cost,
    round(p.avg_cost + (a.total_unrealized_pnl / a.total_position_value * p.market_value / p.shares), 2) as current_price,
    round(p.market_value + (a.total_unrealized_pnl / a.total_position_value * p.market_value), 2) as market_value,
    round((a.total_unrealized_pnl / a.total_position_value * p.market_value), 2) as unrealized_pnl,
    round((a.total_unrealized_pnl / a.total_position_value * 100), 2) as pnl_pct,
    p.ts_ny as last_update
FROM latest_positions p
CROSS JOIN latest_account a
ORDER BY p.market_value DESC
"""
    }],
    "fieldConfig": {
        "overrides": [
            {"matcher": {"id": "byName", "options": "symbol"}, "properties": [{"id": "displayName", "value": "Symbol"}]},
            {"matcher": {"id": "byName", "options": "shares"}, "properties": [{"id": "displayName", "value": "Shares"}, {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "avg_cost"}, "properties": [{"id": "displayName", "value": "Avg Cost"}, {"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "current_price"}, "properties": [{"id": "displayName", "value": "Current"}, {"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "market_value"}, "properties": [{"id": "displayName", "value": "Market Value"}, {"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "unrealized_pnl"}, "properties": [{"id": "displayName", "value": "Unrealized P&L"}, {"id": "unit", "value": "currencyUSD"}, {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
            {"matcher": {"id": "byName", "options": "pnl_pct"}, "properties": [{"id": "displayName", "value": "P&L %"}, {"id": "unit", "value": "percent"}]}
        ]
    }
})
panel_id += 1

# Panel 2.2: Position Distribution Pie
dashboard["panels"].append({
    "type": "piechart",
    "title": "🥧 Position Distribution (持仓分布)",
    "datasource": ds,
    "gridPos": {"x": 16, "y": y, "w": 8, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
WITH latest_positions AS (
    SELECT symbol, market_value
    FROM llm.ibkr_positions_realtime
    WHERE ts_ny = (SELECT max(ts_ny) FROM llm.ibkr_positions_realtime)
      AND position > 0
),
latest_account AS (
    SELECT
        unrealized_pnl as total_unrealized_pnl,
        gross_position_value as total_position_value
    FROM llm.ibkr_account_realtime
    ORDER BY ts_ny DESC
    LIMIT 1
)
SELECT
    p.symbol,
    round(p.market_value + (a.total_unrealized_pnl / a.total_position_value * p.market_value), 2) as value
FROM latest_positions p
CROSS JOIN latest_account a
ORDER BY value DESC
"""
    }],
    "options": {
        "legend": {"displayMode": "table", "placement": "right"},
        "pieType": "donut"
    }
})
panel_id += 1
y += 8

# =============================================================================
# Row 3: 📊 Equity & Performance Curves
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "📊 Equity & Performance (权益与表现)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 3.1: Account Value Over Time
dashboard["panels"].append({
    "type": "timeseries",
    "title": "📈 Account Value History (账户价值历史)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 18, "h": 8},
    "id": panel_id,
    "targets": [
        {
            "refId": "A",
            "rawSql": """
SELECT
    ts_ny as time,
    net_liquidation
FROM llm.ibkr_account_realtime
WHERE ts_ny >= now() - INTERVAL 7 DAY
ORDER BY ts_ny
"""
        },
        {
            "refId": "B",
            "rawSql": """
SELECT
    ts_ny as time,
    total_cash
FROM llm.ibkr_account_realtime
WHERE ts_ny >= now() - INTERVAL 7 DAY
ORDER BY ts_ny
"""
        },
        {
            "refId": "C",
            "rawSql": """
SELECT
    ts_ny as time,
    gross_position_value
FROM llm.ibkr_account_realtime
WHERE ts_ny >= now() - INTERVAL 7 DAY
ORDER BY ts_ny
"""
        }
    ],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "custom": {
                "drawStyle": "line",
                "lineInterpolation": "smooth",
                "fillOpacity": 10
            }
        },
        "overrides": [
            {"matcher": {"id": "byFrameRefID", "options": "A"}, "properties": [{"id": "displayName", "value": "Net Liquidation"}, {"id": "custom.fillOpacity", "value": 25}]},
            {"matcher": {"id": "byFrameRefID", "options": "B"}, "properties": [{"id": "displayName", "value": "Cash"}]},
            {"matcher": {"id": "byFrameRefID", "options": "C"}, "properties": [{"id": "displayName", "value": "Position Value"}]}
        ]
    }
})
panel_id += 1

# Panel 3.2: P&L Gauge
dashboard["panels"].append({
    "type": "gauge",
    "title": "🎯 Current P&L (当前盈亏)",
    "datasource": ds,
    "gridPos": {"x": 18, "y": y, "w": 6, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    unrealized_pnl + realized_pnl as total_pnl
FROM llm.ibkr_account_realtime
ORDER BY ts_ny DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "decimals": 2,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": -2000, "color": "dark-red"},
                    {"value": -1000, "color": "red"},
                    {"value": 0, "color": "yellow"},
                    {"value": 1000, "color": "light-green"},
                    {"value": 2000, "color": "green"}
                ]
            },
            "min": -5000,
            "max": 5000
        }
    }
})
panel_id += 1
y += 8

# =============================================================================
# Row 4: 📋 Trade History
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "📋 Trade History (交易历史)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 4.1: Fills Table
dashboard["panels"].append({
    "type": "table",
    "title": "📝 Recent Fills (近期成交)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 16, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    ts_ny,
    symbol,
    qty as shares,
    price,
    qty * price as total_value,
    fee,
    source
FROM llm.llm_fills
WHERE toDate(ts_ny) >= today() - INTERVAL 7 DAY
  AND symbol LIKE '${symbol}'
ORDER BY ts_ny DESC
LIMIT 100
"""
    }],
    "fieldConfig": {
        "overrides": [
            {"matcher": {"id": "byName", "options": "ts_ny"}, "properties": [{"id": "displayName", "value": "Time"}]},
            {"matcher": {"id": "byName", "options": "symbol"}, "properties": [{"id": "displayName", "value": "Symbol"}]},
            {"matcher": {"id": "byName", "options": "shares"}, "properties": [{"id": "displayName", "value": "Shares"}, {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "price"}, "properties": [{"id": "displayName", "value": "Price"}, {"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "total_value"}, "properties": [{"id": "displayName", "value": "Total Value"}, {"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "fee"}, "properties": [{"id": "displayName", "value": "Fee"}, {"id": "unit", "value": "currencyUSD"}]}
        ]
    }
})
panel_id += 1

# Panel 4.2: Fill Volume by Symbol
dashboard["panels"].append({
    "type": "barchart",
    "title": "📊 Volume by Symbol (各股票成交量)",
    "datasource": ds,
    "gridPos": {"x": 16, "y": y, "w": 8, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    symbol,
    sum(qty * price) as volume
FROM llm.llm_fills
WHERE toDate(ts_ny) >= today() - INTERVAL 7 DAY
  AND symbol LIKE '${symbol}'
GROUP BY symbol
ORDER BY volume DESC
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "decimals": 0
        }
    }
})
panel_id += 1
y += 8

# Save dashboard
output_path = "./grafana/dashboards/active/ai_trading_performance.json"
with open(output_path, "w") as f:
    json.dump(dashboard, f, indent=2)

print("=" * 80)
print("✅ AI Trading Performance Dashboard 已生成!")
print("=" * 80)
print(f"📍 位置: {output_path}")
print("")
print("📊 Dashboard 结构:")
print("")
print("   Row 1: 💵 Today's Performance (9 panels) - ⚠️ CRITICAL FIXES ADDED")
print("      - ⚠️ Data Freshness (新鲜度) - 🔴 P0 修复")
print("      - 📊 Trading Activity (活跃度) - 🔴 P0 修复")
print("      - 💰 Total P&L (账户总盈亏)")
print("      - 📈 Unrealized P&L (浮动盈亏)")
print("      - ✅ Realized P&L (已实现盈亏)")
print("      - 💵 Net Liquidation (净资产)")
print("      - 📊 Open Positions (持仓数)")
print("      - 📝 7-Day Fills (7日成交)")
print("      - 🚦 System Status (系统状态) - 🔴 P0 修复")
print("")
print("   Row 2: 📈 Current Positions (2 panels)")
print("      - Positions Table (持仓明细 with 估算 P&L) - ✅ Already using proportional estimation")
print("      - Position Distribution Pie (持仓分布)")
print("")
print("   Row 3: 📊 Equity & Performance (2 panels)")
print("      - Account Value History (7-day 权益曲线)")
print("      - Current P&L Gauge (当前盈亏仪表盘)")
print("")
print("   Row 4: 📋 Trade History (2 panels)")
print("      - Recent Fills Table (近期成交)")
print("      - Volume by Symbol (各股票成交量)")
print("")
print("=" * 80)
print("✅ 总计: 15 data panels + 4 row headers = 19 components (+3 critical panels)")
print("=" * 80)
print("")
print("🔴 P0 CRITICAL FIXES:")
print("   ✅ Data Freshness Indicator - Shows when data was last updated (color-coded)")
print("   ✅ Trading Activity Monitor - Shows today's fills and last fill time")
print("   ✅ System Status - Overall health check (data age + trading activity)")
print("")
print("🌐 访问: http://localhost:3000/d/ai-trading-performance")
print("")
print("✅ 100% 使用现有数据，无需任何新表或导入脚本！")
print("")
