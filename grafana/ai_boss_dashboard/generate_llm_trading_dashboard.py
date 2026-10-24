#!/usr/bin/env python3
"""
Generate LLM Trading Dashboard - 完整的 LLM 交易数据可视化
Based on llm.* database tables

Dashboard Structure:
- Row 1: 财务概览 (Total Equity, Daily PnL, Cash, Positions Count)
- Row 2: 交易执行 (Today's Fills, Fill Volume, Total Fees)
- Row 3: AI 决策分析 (Decisions Timeline, Action Distribution, Confidence)
- Row 4: 持仓详情 (Current Positions Table, Symbol Distribution)
- Row 5: 权益曲线 (Equity Timeseries, Drawdown)
"""

import json

ds = {"type": "grafana-clickhouse-datasource", "uid": "ch"}

dashboard = {
    "uid": "llm-trading-dashboard",
    "title": "LLM Trading - Complete Overview",
    "description": "LLM 交易系统完整数据监控 - 财务/交易/决策/持仓",
    "tags": ["llm", "trading", "financial"],
    "timezone": "America/New_York",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-24h", "to": "now"},
    "editable": True,

    "templating": {
        "list": [
            {
                "type": "interval",
                "name": "time_range",
                "label": "Time Range",
                "auto": False,
                "current": {"text": "Today", "value": "1d"},
                "options": [
                    {"text": "1h", "value": "1h"},
                    {"text": "6h", "value": "6h"},
                    {"text": "Today", "value": "1d", "selected": True},
                    {"text": "Week", "value": "7d"},
                ]
            },
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
# Row 1: 财务概览 (Financial Overview)
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "💰 Financial Overview (财务概览)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 1.1: Total Equity - BIG NUMBER
dashboard["panels"].append({
    "type": "stat",
    "title": "💵 Total Equity (总权益)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 6, "h": 5},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    anyLast(equity) as current_equity
FROM llm.llm_equity_timeseries
WHERE toDate(ts_ny) = today()
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "decimals": 2,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "red"},
                    {"value": 40000, "color": "yellow"},
                    {"value": 50000, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "graphMode": "area",
        "colorMode": "background"
    }
})
panel_id += 1

# Panel 1.2: Cash Available
dashboard["panels"].append({
    "type": "stat",
    "title": "💵 Cash Available (可用现金)",
    "datasource": ds,
    "gridPos": {"x": 6, "y": y, "w": 6, "h": 5},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    anyLast(cash) as current_cash
FROM llm.llm_equity_timeseries
WHERE toDate(ts_ny) = today()
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

# Panel 1.3: Today's PnL (from daily view)
dashboard["panels"].append({
    "type": "stat",
    "title": "📊 Today's PnL (今日盈亏)",
    "datasource": ds,
    "gridPos": {"x": 12, "y": y, "w": 6, "h": 5},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    realized_pnl + unrealized_pnl as daily_pnl
FROM llm.llm_daily_pnl_view
WHERE trading_day = today()
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
                    {"value": 0, "color": "yellow"},
                    {"value": 1000, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "colorMode": "background"
    }
})
panel_id += 1

# Panel 1.4: Open Positions Count
dashboard["panels"].append({
    "type": "stat",
    "title": "📦 Open Positions (持仓数)",
    "datasource": ds,
    "gridPos": {"x": 18, "y": y, "w": 6, "h": 5},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    count(DISTINCT symbol) as position_count
FROM llm.llm_positions_snapshot
WHERE toDate(ts_ny) = today() AND position > 0
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 0,
            "color": {"mode": "palette-classic"}
        }
    }
})
panel_id += 1
y += 5

# =============================================================================
# Row 2: 交易执行 (Trade Execution)
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "📈 Trade Execution (交易执行)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 2.1: Today's Fills Table
dashboard["panels"].append({
    "type": "table",
    "title": "📋 Today's Fills (今日成交)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 16, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    ts_ny,
    symbol,
    qty,
    price,
    qty * price as total_value,
    fee,
    source
FROM llm.llm_fills
WHERE toDate(ts_ny) = today()
  AND symbol LIKE '${symbol}'
ORDER BY ts_ny DESC
LIMIT 50
"""
    }],
    "fieldConfig": {
        "overrides": [
            {"matcher": {"id": "byName", "options": "ts_ny"}, "properties": [{"id": "displayName", "value": "Time"}]},
            {"matcher": {"id": "byName", "options": "symbol"}, "properties": [{"id": "displayName", "value": "Symbol"}]},
            {"matcher": {"id": "byName", "options": "qty"}, "properties": [{"id": "displayName", "value": "Qty"}, {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "price"}, "properties": [{"id": "displayName", "value": "Price"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]},
            {"matcher": {"id": "byName", "options": "total_value"}, "properties": [{"id": "displayName", "value": "Total Value"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]},
            {"matcher": {"id": "byName", "options": "fee"}, "properties": [{"id": "displayName", "value": "Fee"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 4}]}
        ]
    }
})
panel_id += 1

# Panel 2.2: Fill Statistics
dashboard["panels"].append({
    "type": "stat",
    "title": "📊 Fill Stats (成交统计)",
    "datasource": ds,
    "gridPos": {"x": 16, "y": y, "w": 8, "h": 4},
    "id": panel_id,
    "targets": [
        {
            "refId": "A",
            "rawSql": """
SELECT
    count() as total_fills
FROM llm.llm_fills
WHERE toDate(ts_ny) = today()
"""
        },
        {
            "refId": "B",
            "rawSql": """
SELECT
    sum(qty * price) as total_volume
FROM llm.llm_fills
WHERE toDate(ts_ny) = today()
"""
        },
        {
            "refId": "C",
            "rawSql": """
SELECT
    sum(fee) as total_fees
FROM llm.llm_fills
WHERE toDate(ts_ny) = today()
"""
        }
    ],
    "fieldConfig": {
        "overrides": [
            {"matcher": {"id": "byFrameRefID", "options": "A"}, "properties": [{"id": "displayName", "value": "Total Fills"}, {"id": "unit", "value": "short"}]},
            {"matcher": {"id": "byFrameRefID", "options": "B"}, "properties": [{"id": "displayName", "value": "Total Volume"}, {"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byFrameRefID", "options": "C"}, "properties": [{"id": "displayName", "value": "Total Fees"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]}
        ]
    }
})
panel_id += 1

# Panel 2.3: Fill Timeline
dashboard["panels"].append({
    "type": "timeseries",
    "title": "⏱️ Fill Timeline (成交时间线)",
    "datasource": ds,
    "gridPos": {"x": 16, "y": y + 4, "w": 8, "h": 4},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    ts_ny as time,
    symbol,
    qty
FROM llm.llm_fills
WHERE toDate(ts_ny) = today()
  AND symbol LIKE '${symbol}'
ORDER BY ts_ny
"""
    }],
    "fieldConfig": {
        "defaults": {
            "custom": {
                "drawStyle": "points",
                "pointSize": 8,
                "showPoints": "always"
            }
        }
    }
})
panel_id += 1
y += 8

# =============================================================================
# Row 3: AI 决策分析 (AI Decision Analysis)
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "🤖 AI Decision Analysis (AI 决策分析)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 3.1: Decision Actions Distribution
dashboard["panels"].append({
    "type": "piechart",
    "title": "🎯 Decision Actions (决策分布)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 8, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    JSONExtractString(decision_json, 'action') as action,
    count() as count
FROM llm.llm_decisions
WHERE toDate(ts_ny) = today()
  AND symbol LIKE '${symbol}'
GROUP BY action
ORDER BY count DESC
"""
    }]
})
panel_id += 1

# Panel 3.2: Confidence Distribution
dashboard["panels"].append({
    "type": "histogram",
    "title": "📊 Confidence Distribution (置信度分布)",
    "datasource": ds,
    "gridPos": {"x": 8, "y": y, "w": 8, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    toFloat64(JSONExtractString(decision_json, 'confidence')) as confidence
FROM llm.llm_decisions
WHERE toDate(ts_ny) = today()
  AND symbol LIKE '${symbol}'
  AND JSONExtractString(decision_json, 'confidence') != ''
"""
    }],
    "options": {
        "bucketSize": 0.1
    }
})
panel_id += 1

# Panel 3.3: Recent Decisions Table
dashboard["panels"].append({
    "type": "table",
    "title": "📝 Recent Decisions (最近决策)",
    "datasource": ds,
    "gridPos": {"x": 16, "y": y, "w": 8, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    ts_ny,
    symbol,
    JSONExtractString(decision_json, 'action') as action,
    toFloat64(JSONExtractString(decision_json, 'confidence')) as confidence,
    substring(JSONExtractString(decision_json, 'rationale'), 1, 50) as rationale
FROM llm.llm_decisions
WHERE toDate(ts_ny) = today()
  AND symbol LIKE '${symbol}'
ORDER BY ts_ny DESC
LIMIT 20
"""
    }],
    "fieldConfig": {
        "overrides": [
            {"matcher": {"id": "byName", "options": "ts_ny"}, "properties": [{"id": "displayName", "value": "Time"}]},
            {"matcher": {"id": "byName", "options": "symbol"}, "properties": [{"id": "displayName", "value": "Symbol"}]},
            {"matcher": {"id": "byName", "options": "action"}, "properties": [{"id": "displayName", "value": "Action"}]},
            {"matcher": {"id": "byName", "options": "confidence"}, "properties": [{"id": "displayName", "value": "Confidence"}, {"id": "decimals", "value": 2}]},
            {"matcher": {"id": "byName", "options": "rationale"}, "properties": [{"id": "displayName", "value": "Rationale (Preview)"}]}
        ]
    }
})
panel_id += 1
y += 8

# =============================================================================
# Row 4: 持仓详情 (Position Details)
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "📦 Position Details (持仓详情)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 4.1: Current Positions Table
dashboard["panels"].append({
    "type": "table",
    "title": "📋 Current Positions (当前持仓)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 16, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    p.symbol,
    anyLast(p.position) as position,
    anyLast(p.avg_cost) as avg_cost,
    anyLast(p.market_price) as current_price,
    anyLast(p.unrealized_pnl) as unrealized_pnl,
    anyLast(p.position) * anyLast(p.market_price) as market_value,
    anyLast(p.realized_pnl_to_date) as realized_pnl
FROM llm.llm_positions_snapshot p
WHERE symbol LIKE '${symbol}'
GROUP BY symbol
HAVING anyLast(p.position) > 0
ORDER BY market_value DESC
"""
    }],
    "fieldConfig": {
        "overrides": [
            {"matcher": {"id": "byName", "options": "symbol"}, "properties": [{"id": "displayName", "value": "Symbol"}]},
            {"matcher": {"id": "byName", "options": "position"}, "properties": [{"id": "displayName", "value": "Shares"}, {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "avg_cost"}, "properties": [{"id": "displayName", "value": "Avg Cost"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]},
            {"matcher": {"id": "byName", "options": "current_price"}, "properties": [{"id": "displayName", "value": "Current Price"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]},
            {"matcher": {"id": "byName", "options": "unrealized_pnl"}, "properties": [{"id": "displayName", "value": "Unrealized PnL"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]},
            {"matcher": {"id": "byName", "options": "market_value"}, "properties": [{"id": "displayName", "value": "Market Value"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]},
            {"matcher": {"id": "byName", "options": "realized_pnl"}, "properties": [{"id": "displayName", "value": "Realized PnL"}, {"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]}
        ]
    }
})
panel_id += 1

# Panel 4.2: Position Distribution (Pie Chart)
dashboard["panels"].append({
    "type": "piechart",
    "title": "🥧 Position Distribution (持仓分布)",
    "datasource": ds,
    "gridPos": {"x": 16, "y": y, "w": 8, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    symbol,
    anyLast(position * market_price) as market_value
FROM llm.llm_positions_snapshot
WHERE symbol LIKE '${symbol}'
GROUP BY symbol
HAVING anyLast(position) > 0
ORDER BY market_value DESC
"""
    }]
})
panel_id += 1
y += 8

# =============================================================================
# Row 5: 权益曲线 (Equity Curve)
# =============================================================================
dashboard["panels"].append({
    "type": "row",
    "title": "📈 Equity Curve (权益曲线)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 5.1: Equity Timeseries
dashboard["panels"].append({
    "type": "timeseries",
    "title": "📈 Equity Timeseries (权益走势)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 18, "h": 8},
    "id": panel_id,
    "targets": [
        {
            "refId": "A",
            "rawSql": """
SELECT
    ts_ny as time,
    equity
FROM llm.llm_equity_timeseries
WHERE toDate(ts_ny) = today()
ORDER BY ts_ny
"""
        },
        {
            "refId": "B",
            "rawSql": """
SELECT
    ts_ny as time,
    cash
FROM llm.llm_equity_timeseries
WHERE toDate(ts_ny) = today()
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
            {"matcher": {"id": "byFrameRefID", "options": "A"}, "properties": [{"id": "displayName", "value": "Equity"}]},
            {"matcher": {"id": "byFrameRefID", "options": "B"}, "properties": [{"id": "displayName", "value": "Cash"}]}
        ]
    }
})
panel_id += 1

# Panel 5.2: Drawdown Gauge
dashboard["panels"].append({
    "type": "gauge",
    "title": "⚠️ Drawdown (回撤)",
    "datasource": ds,
    "gridPos": {"x": 18, "y": y, "w": 6, "h": 8},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    anyLast(drawdown_to_date) as current_drawdown
FROM llm.llm_equity_timeseries
WHERE toDate(ts_ny) = today()
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "percentunit",
            "decimals": 2,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "green"},
                    {"value": 0.05, "color": "yellow"},
                    {"value": 0.10, "color": "orange"},
                    {"value": 0.20, "color": "red"}
                ]
            },
            "max": 0.3,
            "min": 0
        }
    }
})
panel_id += 1

# Save dashboard
output_path = "./grafana/dashboards/active/llm_trading_dashboard.json"
with open(output_path, "w") as f:
    json.dump(dashboard, f, indent=2)

print("✅ LLM Trading Dashboard 已生成!")
print(f"📍 位置: {output_path}")
print("")
print("📊 Dashboard 结构:")
print("   Row 1: 财务概览 (4 panels) - Total Equity, Cash, Today's PnL, Position Count")
print("   Row 2: 交易执行 (3 panels) - Fills Table, Fill Stats, Timeline")
print("   Row 3: AI 决策分析 (3 panels) - Action Distribution, Confidence, Recent Decisions")
print("   Row 4: 持仓详情 (2 panels) - Positions Table, Distribution Chart")
print("   Row 5: 权益曲线 (2 panels) - Equity Timeseries, Drawdown Gauge")
print("")
print("✅ 总计: 14 data panels + 5 row headers = 19 components")
print("")
print("📊 数据源:")
print("   - llm.llm_equity_timeseries (权益时间序列)")
print("   - llm.llm_fills (交易填充)")
print("   - llm.llm_decisions (AI 决策)")
print("   - llm.llm_positions_snapshot (持仓快照)")
print("   - llm.llm_daily_pnl_view (日盈亏视图)")
print("")
print("🌐 访问: http://localhost:3000/d/llm-trading-dashboard")
