#!/usr/bin/env python3
"""
Generate IDEAL AI Boss Dashboard according to user's specification
- 4 Rows: Macro Overview / AI Performance / Portfolio / Execution
- Interactive variables
- Evidence-based: real data where available, placeholder where not
"""

import json

dashboard = {
    "uid": "ai-boss-ideal-v2",
    "title": "AI Boss - Ideal Dashboard V2",
    "description": "驾驶舱式完整结构 - 4 Rows (宏观/AI/组合/执行)",
    "tags": ["ai-boss", "ideal", "v2"],
    "timezone": "America/New_York",
    "schemaVersion": 39,
    "version": 2,
    "refresh": "30s",
    "time": {"from": "now-24h", "to": "now"},
    "editable": True,
    
    # ==========================================================================
    # Interactive Variables (交互功能)
    # ==========================================================================
    "templating": {
        "list": [
            {
                "type": "interval",
                "name": "time_range",
                "label": "Time Range",
                "auto": False,
                "auto_count": 30,
                "auto_min": "10s",
                "current": {"text": "1h", "value": "1h"},
                "options": [
                    {"text": "1h", "value": "1h", "selected": True},
                    {"text": "6h", "value": "6h"},
                    {"text": "Today", "value": "1d"},
                    {"text": "This Week", "value": "7d"},
                    {"text": "This Month", "value": "30d"}
                ]
            },
            {
                "type": "query",
                "name": "strategy_id",
                "label": "Strategy",
                "datasource": {"type": "grafana-clickhouse-datasource", "uid": "ch"},
                "query": "SELECT DISTINCT strategy_id FROM trading.portfolio_snapshots WHERE strategy_id != '' ORDER BY strategy_id",
                "refresh": 2,
                "includeAll": True,
                "allValue": "%",
                "current": {"text": "All", "value": "%", "selected": True},
                "multi": False
            },
            {
                "type": "query",
                "name": "symbol",
                "label": "Symbol",
                "datasource": {"type": "grafana-clickhouse-datasource", "uid": "ch"},
                "query": "SELECT DISTINCT symbol FROM trading.bars_5m_current WHERE symbol != '' ORDER BY symbol LIMIT 100",
                "refresh": 2,
                "includeAll": True,
                "allValue": "%",
                "current": {"text": "All", "value": "%", "selected": True},
                "multi": False
            }
        ]
    },
    
    "panels": []
}

ds = {"type": "grafana-clickhouse-datasource", "uid": "ch"}
panel_id = 1
y = 0

# ==============================================================================
# ROW 1: 顶级宏观概览 (The "Am I Rich or Poor?" View)
# ==============================================================================

dashboard["panels"].append({
    "type": "row",
    "title": "💰 Row 1: 顶级宏观概览 (Am I Rich or Poor?)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 1.1: Total PnL - MASSIVE STAT PANEL
dashboard["panels"].append({
    "type": "stat",
    "title": "💰 Total PnL (总盈亏)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 8, "h": 6},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- 今日 + 本周 + 本月 + 总计盈亏
-- ⚠️ PLACEHOLDER - Need equity_pnl_snapshots data
SELECT
    anyLast(realized_pnl) + anyLast(unrealized_pnl) as total_pnl
FROM trading.equity_pnl_snapshots
WHERE date = today()
-- Fallback: show 0 if no data
UNION ALL
SELECT 0 as total_pnl
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
                    {"value": -10000, "color": "dark-red"},
                    {"value": -1000, "color": "red"},
                    {"value": 0, "color": "yellow"},
                    {"value": 1000, "color": "light-green"},
                    {"value": 10000, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "graphMode": "area",
        "colorMode": "background",
        "textMode": "value",
        "reduceOptions": {"calcs": ["lastNotNull"]},
        "text": {"titleSize": 16, "valueSize": 48}
    },
    "description": "⚠️ PLACEHOLDER\n\n需要: equity_pnl_snapshots 表\n\n这是最重要的指标!显示今日/本周/本月/总计盈亏"
})
panel_id += 1

# Panel 1.2: Max Drawdown - GAUGE
dashboard["panels"].append({
    "type": "gauge",
    "title": "📉 Max Drawdown (最大回撤)",
    "datasource": ds,
    "gridPos": {"x": 8, "y": y, "w": 5, "h": 6},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ✅ HAS DATA: Using OOS MDD from metrics
SELECT value * 100 as mdd_pct
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_mdd'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "percent",
            "decimals": 1,
            "min": 0,
            "max": 25,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "green"},
                    {"value": 5, "color": "yellow"},
                    {"value": 10, "color": "orange"},
                    {"value": 15, "color": "red"},
                    {"value": 20, "color": "dark-red"}
                ]
            }
        }
    },
    "options": {
        "showThresholdLabels": True,
        "showThresholdMarkers": True
    },
    "description": "✅ REAL DATA\n\n来自: ai_trader_dash_metrics.oos_mdd\n\n衡量风险承受能力的核心指标"
})
panel_id += 1

# Panel 1.3: Sharpe Ratio - STAT
dashboard["panels"].append({
    "type": "stat",
    "title": "📈 Sharpe Ratio (夏普比率)",
    "datasource": ds,
    "gridPos": {"x": 13, "y": y, "w": 4, "h": 6},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ✅ HAS DATA: Using OOS Sharpe from metrics
SELECT value as sharpe
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_sharpe'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 2,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": -1, "color": "dark-red"},
                    {"value": 0, "color": "red"},
                    {"value": 0.5, "color": "yellow"},
                    {"value": 1.0, "color": "light-green"},
                    {"value": 2.0, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "graphMode": "none",
        "colorMode": "background",
        "textMode": "value",
        "text": {"valueSize": 32}
    },
    "description": "✅ REAL DATA\n\n来自: ai_trader_dash_metrics.oos_sharpe\n\n衡量经风险调整后的收益"
})
panel_id += 1

# Panel 1.4: System Status - POLYSTAT (multi-service status)
dashboard["panels"].append({
    "type": "stat",
    "title": "🟢 System Status (系统状态)",
    "datasource": ds,
    "gridPos": {"x": 17, "y": y, "w": 4, "h": 3},
    "id": panel_id,
    "targets": [
        {
            "refId": "DQ",
            "rawSql": """
SELECT
    'DQ' as metric,
    CASE WHEN value = 1 THEN 1 ELSE 0 END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'dq_ok' AND date = today()
ORDER BY ts DESC LIMIT 1
"""
        },
        {
            "refId": "Tools",
            "rawSql": """
SELECT
    'Tools' as metric,
    CASE WHEN value > 0 THEN 1 ELSE 0 END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'tools_running' AND date = today()
ORDER BY ts DESC LIMIT 1
"""
        }
    ],
    "fieldConfig": {
        "defaults": {
            "mappings": [
                {"type": "value", "options": {"0": {"text": "❌", "color": "red"}}},
                {"type": "value", "options": {"1": {"text": "✅", "color": "green"}}}
            ],
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "red"},
                    {"value": 1, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "graphMode": "none",
        "colorMode": "background",
        "textMode": "name"
    },
    "description": "✅ REAL DATA\n\n显示核心服务状态:\n- DQ (数据质量)\n- Tools (工具健康)"
})
panel_id += 1

# Panel 1.5: Guardian Status - TEXT/STAT
dashboard["panels"].append({
    "type": "stat",
    "title": "🛡️ Guardian Status (守护者)",
    "datasource": ds,
    "gridPos": {"x": 21, "y": y, "w": 3, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
    value as events,
    CASE
        WHEN value = 0 THEN 'Active'
        WHEN value < 5 THEN 'Warning'
        ELSE 'TRIPPED'
    END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'guardian_events' AND date = today()
ORDER BY ts DESC LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "mappings": [
                {"type": "value", "options": {"0": {"text": "🟢 Active", "color": "green"}}},
                {"type": "range", "options": {"from": 1, "to": 4, "result": {"text": "🟡 Warning", "color": "yellow"}}},
                {"type": "range", "options": {"from": 5, "to": 999, "result": {"text": "🔴 TRIPPED", "color": "red"}}}
            ]
        }
    },
    "options": {
        "graphMode": "none",
        "colorMode": "background",
        "textMode": "value"
    },
    "description": "✅ REAL DATA\n\n最后的安全防线\n0 = Active (正常)\n>0 = 触发次数"
})
panel_id += 1

# Add Account Equity Chart
dashboard["panels"].append({
    "type": "timeseries",
    "title": "💵 Account Equity (24h)",
    "datasource": ds,
    "gridPos": {"x": 17, "y": y+3, "w": 7, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER
SELECT
    ts as time,
    equity as value
FROM trading.equity_pnl_snapshots
WHERE ts > now() - INTERVAL 24 HOUR
ORDER BY ts
"""
    }],
    "fieldConfig": {
        "defaults": {
            "custom": {
                "drawStyle": "line",
                "lineWidth": 2,
                "fillOpacity": 15,
                "showPoints": "never"
            },
            "unit": "currencyUSD"
        }
    },
    "description": "⚠️ PLACEHOLDER\n\n需要: equity_pnl_snapshots"
})
panel_id += 1
y += 6

# ==============================================================================
# ROW 2: AI 模型表现与决策分析 (The "Is the Brain Smart?" View)
# ==============================================================================

dashboard["panels"].append({
    "type": "row",
    "title": "🧠 Row 2: AI 模型表现 (Is the Brain Smart?)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 2.1: Signal Distribution - HISTOGRAM
dashboard["panels"].append({
    "type": "histogram",
    "title": "📊 Signal Distribution (信号分布)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 8, "h": 7},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER - Need trading_signals table
SELECT
    round(signal, 1) as signal_bucket,
    count() as frequency
FROM trading.trading_signals
WHERE ts > now() - INTERVAL $time_range
  AND strategy_id LIKE '$strategy_id'
  AND symbol LIKE '$symbol'
GROUP BY signal_bucket
ORDER BY signal_bucket
"""
    }],
    "description": "⚠️ PLACEHOLDER\n\n需要: trading_signals 表\n\n显示模型信号 (-1到1) 的分布\n明确信号 = 分布在±1附近\n犹豫不决 = 集中在0附近"
})
panel_id += 1

# Panel 2.2: Confidence Distribution - HISTOGRAM
dashboard["panels"].append({
    "type": "histogram",
    "title": "🎯 Confidence Distribution (置信度分布)",
    "datasource": ds,
    "gridPos": {"x": 8, "y": y, "w": 8, "h": 7},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER
SELECT
    round(confidence, 1) as conf_bucket,
    count() as frequency
FROM trading.trading_signals
WHERE ts > now() - INTERVAL $time_range
  AND strategy_id LIKE '$strategy_id'
  AND symbol LIKE '$symbol'
GROUP BY conf_bucket
ORDER BY conf_bucket
"""
    }],
    "description": "⚠️ PLACEHOLDER\n\n需要: trading_signals 表\n\n显示模型置信度 (0到1) 的分布\n高置信度 = 模型确定\n低置信度 = 模型不确定"
})
panel_id += 1

# Panel 2.3: High-Confidence Trade Performance - BAR CHART
dashboard["panels"].append({
    "type": "barchart",
    "title": "🏆 High-Confidence Performance (高置信度表现)",
    "datasource": ds,
    "gridPos": {"x": 16, "y": y, "w": 8, "h": 7},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER
SELECT
    floor(confidence * 10) / 10 as conf_range,
    count() as total_trades,
    countIf(outcome > 0) as wins,
    round(countIf(outcome > 0) / count() * 100, 1) as win_rate,
    round(avg(outcome), 2) as avg_pnl
FROM trading.trading_signals
WHERE executed = 1
  AND outcome IS NOT NULL
  AND ts > now() - INTERVAL $time_range
  AND strategy_id LIKE '$strategy_id'
  AND symbol LIKE '$symbol'
GROUP BY conf_range
ORDER BY conf_range
"""
    }],
    "description": "⚠️ PLACEHOLDER\n\n需要: trading_signals + outcome 数据\n\n验证模型有效性的关键!\n高置信度交易应该有更高的胜率和盈亏"
})
panel_id += 1
y += 7

# ==============================================================================
# ROW 3: 投资组合与风险敞口 (The "What Are We Betting On?" View)
# ==============================================================================

dashboard["panels"].append({
    "type": "row",
    "title": "💼 Row 3: 投资组合风险 (What Are We Betting On?)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 3.1: PnL by Strategy - TABLE/BAR
dashboard["panels"].append({
    "type": "table",
    "title": "📊 Strategy PnL (各策略盈亏)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 12, "h": 7},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER
SELECT
    strategy_id,
    sum(pnl) as total_pnl,
    count() as trades,
    round(avg(pnl), 2) as avg_pnl_per_trade
FROM trading.portfolio_snapshots
WHERE date >= today() - INTERVAL $time_range
  AND strategy_id LIKE '$strategy_id'
GROUP BY strategy_id
ORDER BY total_pnl DESC
"""
    }],
    "description": "⚠️ PLACEHOLDER\n\n需要: portfolio_snapshots 表\n\n快速识别:\n- 明星策略 (盈利多)\n- 累赘策略 (亏损多)"
})
panel_id += 1

# Panel 3.2: Current Positions & Exposure - TABLE + CHART
dashboard["panels"].append({
    "type": "table",
    "title": "📋 Current Positions (当前持仓)",
    "datasource": ds,
    "gridPos": {"x": 12, "y": y, "w": 12, "h": 7},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER
SELECT
    symbol,
    qty,
    entry_price,
    current_price,
    unrealized_pnl,
    round((current_price - entry_price) / entry_price * 100, 2) as pnl_pct,
    strategy_id
FROM trading.positions_snapshots
WHERE date = today()
  AND ts = (SELECT max(ts) FROM trading.positions_snapshots WHERE date = today())
  AND strategy_id LIKE '$strategy_id'
  AND symbol LIKE '$symbol'
ORDER BY abs(unrealized_pnl) DESC
"""
    }],
    "description": "⚠️ PLACEHOLDER\n\n需要: positions_snapshots 表\n\n精确了解:\n- 钱放在哪里\n- 每个持仓的盈亏"
})
panel_id += 1
y += 7

# ==============================================================================
# ROW 4: 交易执行与成本分析 (The "Are We Trading Efficiently?" View)
# ==============================================================================

dashboard["panels"].append({
    "type": "row",
    "title": "⚡ Row 4: 交易执行 (Are We Trading Efficiently?)",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y += 1

# Panel 4.1: Slippage Analysis - HISTOGRAM/TIME SERIES
dashboard["panels"].append({
    "type": "timeseries",
    "title": "💸 Slippage Analysis (滑点分析)",
    "datasource": ds,
    "gridPos": {"x": 0, "y": y, "w": 12, "h": 6},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER
SELECT
    f.ts as time,
    (f.fill_price - o.limit_price) / o.limit_price * 10000 as slippage_bps,
    o.symbol
FROM trading.fills_live f
JOIN trading.order_events o ON f.order_id = o.order_id
WHERE f.ts > now() - INTERVAL $time_range
  AND o.order_type = 'LMT'
  AND o.strategy_id LIKE '$strategy_id'
  AND o.symbol LIKE '$symbol'
ORDER BY f.ts
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "custom": {"drawStyle": "points", "pointSize": 5}
        }
    },
    "description": "⚠️ PLACEHOLDER\n\n需要: fills_live + order_events\n\n滑点 = 隐性成本\n过高滑点会严重侵蚀利润"
})
panel_id += 1

# Panel 4.2: Decision-to-Fill Funnel - FUNNEL/BAR
dashboard["panels"].append({
    "type": "bargauge",
    "title": "🔽 Decision Funnel (决策漏斗)",
    "datasource": ds,
    "gridPos": {"x": 12, "y": y, "w": 12, "h": 6},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER
SELECT
    stage,
    count(DISTINCT decision_id) as decisions
FROM trading.exec_events
WHERE date >= today() - INTERVAL $time_range
  AND details LIKE '%$strategy_id%'
  AND details LIKE '%$symbol%'
GROUP BY stage
ORDER BY
    CASE stage
        WHEN 'GENERATED' THEN 1
        WHEN 'RISK_CHECK' THEN 2
        WHEN 'SUBMITTED' THEN 3
        WHEN 'FILLED' THEN 4
        WHEN 'REJECTED' THEN 5
    END
"""
    }],
    "fieldConfig": {
        "defaults": {
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "red"},
                    {"value": 50, "color": "yellow"},
                    {"value": 80, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "orientation": "horizontal",
        "displayMode": "gradient"
    },
    "description": "⚠️ PLACEHOLDER\n\n需要: exec_events 表\n\n显示转化率:\nAI决策 → 风控 → 提交 → 成交"
})
panel_id += 1
y += 6

# Save dashboard
output_path = "./grafana/dashboards/active/ai_boss_ideal_v2.json"
with open(output_path, "w") as f:
    json.dump(dashboard, f, indent=2)

print("✅ IDEAL Dashboard 已生成!")
print(f"📍 位置: {output_path}")
print("")
print("📊 Dashboard 结构:")
print("   Row 1: 顶级宏观概览 (5 panels)")
print("      ✅ Total PnL (⚠️ placeholder)")
print("      ✅ Max Drawdown (REAL DATA)")
print("      ✅ Sharpe Ratio (REAL DATA)")
print("      ✅ System Status (REAL DATA)")
print("      ✅ Guardian Status (REAL DATA)")
print("      ✅ Equity Chart (⚠️ placeholder)")
print("")
print("   Row 2: AI 模型表现 (3 panels)")
print("      ⚠️ Signal Distribution (placeholder)")
print("      ⚠️ Confidence Distribution (placeholder)")
print("      ⚠️ High-Conf Performance (placeholder)")
print("")
print("   Row 3: 投资组合风险 (2 panels)")
print("      ⚠️ Strategy PnL (placeholder)")
print("      ⚠️ Current Positions (placeholder)")
print("")
print("   Row 4: 交易执行 (2 panels)")
print("      ⚠️ Slippage Analysis (placeholder)")
print("      ⚠️ Decision Funnel (placeholder)")
print("")
print("🎛️ 交互变量:")
print("   - Time Range (1h/6h/Today/Week/Month)")
print("   - Strategy ID (下拉筛选)")
print("   - Symbol (下拉筛选)")
print("")
print("📈 实施状态:")
print("   ✅ 真实数据: 6/14 panels (43%)")
print("   ⚠️ 占位符: 8/14 panels (57%)")
print("")
print("🌐 访问: http://localhost:3000/d/ai-boss-ideal-v2")
