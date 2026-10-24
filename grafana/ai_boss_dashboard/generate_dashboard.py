#!/usr/bin/env python3
"""
Generate AI Boss Dashboard with all 15 panels
- 8 panels with real data
- 7 panels as placeholders
"""

import json
from datetime import datetime

# Dashboard base structure
dashboard = {
    "uid": "ai-boss-command-center",
    "title": "AI Boss - Trading Command Center",
    "description": "Executive dashboard for AI trading system monitoring",
    "tags": ["ai-boss", "executive", "monitoring"],
    "timezone": "America/New_York",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {
        "from": "now-24h",
        "to": "now"
    },
    "editable": True,
    "panels": []
}

# Datasource configuration
clickhouse_ds = {
    "type": "grafana-clickhouse-datasource",
    "uid": "ch"
}

panel_id = 1
y_pos = 0

# =============================================================================
# ROW 1: Executive Summary
# =============================================================================

# Row 1 Title
dashboard["panels"].append({
    "type": "row",
    "title": "📊 Executive Summary",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y_pos, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y_pos += 1

# Panel 1.1: Total PnL (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "Total PnL ⏳",
    "gridPos": {"x": 0, "y": y_pos, "w": 5, "h": 4},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for live trading data

**Required**: `equity_pnl_snapshots` table

**Query Preview**:
```sql
SELECT 
  anyLast(equity) - anyFirst(equity) as total_pnl
FROM trading.equity_pnl_snapshots
WHERE date = today()
```

**Action Required**: 
Log equity snapshots during live trading
"""
    }
})
panel_id += 1

# Panel 1.2: Max Drawdown (IMPLEMENTED)
dashboard["panels"].append({
    "type": "gauge",
    "title": "OOS Max Drawdown",
    "datasource": clickhouse_ds,
    "gridPos": {"x": 5, "y": y_pos, "w": 5, "h": 4},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT value * 100 as drawdown_pct
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
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "green"},
                    {"value": 5, "color": "yellow"},
                    {"value": 10, "color": "orange"},
                    {"value": 15, "color": "red"}
                ]
            },
            "min": 0,
            "max": 20
        }
    },
    "options": {
        "showThresholdLabels": True,
        "showThresholdMarkers": True
    }
})
panel_id += 1

# Panel 1.3: Sharpe Ratio (IMPLEMENTED)
dashboard["panels"].append({
    "type": "stat",
    "title": "OOS Sharpe Ratio",
    "datasource": clickhouse_ds,
    "gridPos": {"x": 10, "y": y_pos, "w": 4, "h": 4},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
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
                    {"value": 0, "color": "red"},
                    {"value": 0.5, "color": "yellow"},
                    {"value": 1.0, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "graphMode": "none",
        "colorMode": "background"
    }
})
panel_id += 1

# Panel 1.4: System Status (IMPLEMENTED)
dashboard["panels"].append({
    "type": "stat",
    "title": "System Status",
    "datasource": clickhouse_ds,
    "gridPos": {"x": 14, "y": y_pos, "w": 5, "h": 4},
    "id": panel_id,
    "targets": [
        {
            "refId": "A",
            "rawSql": """
SELECT 
  'Data Quality' as metric,
  CASE WHEN value = 1 THEN 'OK' ELSE 'FAILED' END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'dq_ok' AND date = today()
ORDER BY ts DESC LIMIT 1
"""
        },
        {
            "refId": "B",
            "rawSql": """
SELECT 
  'Tools' as metric,
  CASE WHEN value > 0 THEN 'RUNNING' ELSE 'STOPPED' END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'tools_running' AND date = today()
ORDER BY ts DESC LIMIT 1
"""
        }
    ],
    "options": {
        "graphMode": "none",
        "colorMode": "value",
        "textMode": "value_and_name"
    }
})
panel_id += 1

# Panel 1.5: Guardian Status (IMPLEMENTED)
dashboard["panels"].append({
    "type": "stat",
    "title": "Guardian Status",
    "datasource": clickhouse_ds,
    "gridPos": {"x": 19, "y": y_pos, "w": 5, "h": 4},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT value as event_count
FROM trading.ai_trader_dash_metrics
WHERE name = 'guardian_events'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 0,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": 0, "color": "green"},
                    {"value": 1, "color": "yellow"},
                    {"value": 5, "color": "red"}
                ]
            },
            "mappings": [{
                "type": "value",
                "options": {
                    "0": {"text": "🟢 Active"}
                }
            }]
        }
    },
    "options": {
        "graphMode": "none",
        "colorMode": "background",
        "textMode": "value_and_name"
    }
})
panel_id += 1
y_pos += 4

# =============================================================================
# ROW 2: AI Brain Intelligence
# =============================================================================

dashboard["panels"].append({
    "type": "row",
    "title": "🧠 AI Brain Intelligence",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y_pos, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y_pos += 1

# Panel 2.1: Signal Distribution (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "Signal Distribution ⏳",
    "gridPos": {"x": 0, "y": y_pos, "w": 6, "h": 6},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for signal logging

**Required**: Trading signals table

**Query Preview**:
```sql
SELECT 
  round(signal, 1) as signal_bucket,
  count() as frequency
FROM trading.trading_signals
WHERE ts > now() - INTERVAL 24 HOUR
GROUP BY signal_bucket
ORDER BY signal_bucket
```

**Action Required**: 
Log signal values (-1 to 1) for each trade decision
"""
    }
})
panel_id += 1

# Panel 2.2: Confidence Distribution (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "Confidence Distribution ⏳",
    "gridPos": {"x": 6, "y": y_pos, "w": 6, "h": 6},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for confidence logging

**Required**: Trading confidence data

**Query Preview**:
```sql
SELECT 
  round(confidence, 1) as conf_bucket,
  count() as frequency
FROM trading.trading_signals
WHERE ts > now() - INTERVAL 24 HOUR
GROUP BY conf_bucket
ORDER BY conf_bucket
```

**Action Required**: 
Log confidence scores (0 to 1) for each trade decision
"""
    }
})
panel_id += 1

# Panel 2.3: Brain Calls (24h) - IMPLEMENTED
dashboard["panels"].append({
    "type": "stat",
    "title": "Brain Calls (24h)",
    "datasource": clickhouse_ds,
    "gridPos": {"x": 12, "y": y_pos, "w": 4, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT count() as brain_calls
FROM trading.ai_trader_events
WHERE kind = 'brain'
  AND ts > now() - INTERVAL 24 HOUR
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 0,
            "color": {"mode": "thresholds"},
            "thresholds": {
                "mode": "absolute",
                "steps": [{"value": 0, "color": "blue"}]
            }
        }
    },
    "options": {
        "graphMode": "area",
        "colorMode": "background"
    }
})
panel_id += 1

# Panel 2.4: Tool Executions (24h) - IMPLEMENTED
dashboard["panels"].append({
    "type": "stat",
    "title": "Tool Executions (24h)",
    "datasource": clickhouse_ds,
    "gridPos": {"x": 16, "y": y_pos, "w": 4, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT count() as tool_executions
FROM trading.ai_trader_events
WHERE kind = 'tool'
  AND ts > now() - INTERVAL 24 HOUR
"""
    }],
    "fieldConfig": {
        "defaults": {
            "unit": "short",
            "decimals": 0,
            "color": {"mode": "thresholds"},
            "thresholds": {
                "mode": "absolute",
                "steps": [{"value": 0, "color": "purple"}]
            }
        }
    },
    "options": {
        "graphMode": "area",
        "colorMode": "background"
    }
})
panel_id += 1

# Panel 2.5: OOS Performance Table - IMPLEMENTED
dashboard["panels"].append({
    "type": "table",
    "title": "OOS Performance Metrics",
    "datasource": clickhouse_ds,
    "gridPos": {"x": 20, "y": y_pos, "w": 4, "h": 6},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
WITH latest AS (
  SELECT name, value,
    ROW_NUMBER() OVER (PARTITION BY name ORDER BY ts DESC) as rn
  FROM trading.ai_trader_dash_metrics
  WHERE name IN ('oos_sharpe', 'oos_mdd', 'oos_trades')
    AND date = today()
)
SELECT
  CASE
    WHEN name = 'oos_sharpe' THEN 'Sharpe'
    WHEN name = 'oos_mdd' THEN 'MDD'
    WHEN name = 'oos_trades' THEN 'Trades'
  END as metric,
  CASE
    WHEN name = 'oos_mdd' THEN round(value * 100, 1)
    ELSE round(value, 2)
  END as value
FROM latest
WHERE rn = 1
ORDER BY name
"""
    }]
})
panel_id += 1

# Panel 2.6: Brain Call Frequency - IMPLEMENTED
dashboard["panels"].append({
    "type": "timeseries",
    "title": "Brain Call Frequency",
    "datasource": clickhouse_ds,
    "gridPos": {"x": 12, "y": y_pos + 3, "w": 8, "h": 3},
    "id": panel_id,
    "targets": [{
        "refId": "A",
        "rawSql": """
SELECT
  toStartOfHour(ts) as time,
  session,
  count() as calls
FROM trading.ai_trader_events
WHERE kind = 'brain'
  AND ts > $__timeFrom()
  AND ts < $__timeTo()
GROUP BY time, session
ORDER BY time
"""
    }],
    "fieldConfig": {
        "defaults": {
            "custom": {
                "drawStyle": "line",
                "lineWidth": 2,
                "fillOpacity": 10
            },
            "unit": "short"
        }
    }
})
panel_id += 1
y_pos += 6

# =============================================================================
# ROW 3: Portfolio (ALL PLACEHOLDERS)
# =============================================================================

dashboard["panels"].append({
    "type": "row",
    "title": "💼 Portfolio",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y_pos, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y_pos += 1

# Panel 3.1: PnL by Strategy (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "PnL by Strategy ⏳",
    "gridPos": {"x": 0, "y": y_pos, "w": 8, "h": 6},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for live trading

**Required**: `portfolio_snapshots` table

**Query Preview**:
```sql
SELECT
  strategy_id,
  sum(pnl) as total_pnl
FROM trading.portfolio_snapshots
WHERE date = today()
GROUP BY strategy_id
ORDER BY total_pnl DESC
```

**Action Required**: 
Log portfolio snapshots during live trading
"""
    }
})
panel_id += 1

# Panel 3.2: Current Positions (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "Current Positions ⏳",
    "gridPos": {"x": 8, "y": y_pos, "w": 8, "h": 6},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for live trading

**Required**: `positions_snapshots` table

**Query Preview**:
```sql
SELECT
  symbol, qty, entry_price,
  current_price, pnl
FROM trading.positions_snapshots
WHERE date = today()
ORDER BY abs(pnl) DESC
```

**Action Required**: 
Log position snapshots during live trading
"""
    }
})
panel_id += 1

# Panel 3.3: Exposure Breakdown (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "Exposure Breakdown ⏳",
    "gridPos": {"x": 16, "y": y_pos, "w": 8, "h": 6},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for live trading

**Required**: `positions_snapshots` table

**Query Preview**:
```sql
SELECT
  sector,
  sum(notional) as exposure
FROM trading.positions_snapshots
WHERE date = today()
GROUP BY sector
```

**Action Required**: 
Log position details with sector info
"""
    }
})
panel_id += 1
y_pos += 6

# =============================================================================
# ROW 4: Execution Quality (ALL PLACEHOLDERS)
# =============================================================================

dashboard["panels"].append({
    "type": "row",
    "title": "⚡ Execution Quality",
    "collapsed": False,
    "gridPos": {"x": 0, "y": y_pos, "w": 24, "h": 1},
    "id": panel_id
})
panel_id += 1
y_pos += 1

# Panel 4.1: Avg Slippage (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "Avg Slippage ⏳",
    "gridPos": {"x": 0, "y": y_pos, "w": 8, "h": 6},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for execution data

**Required**: `fills_live` + `order_events`

**Query Preview**:
```sql
SELECT
  avg((fill_price - limit_price) / limit_price * 10000) as slippage_bps
FROM trading.fills_live f
JOIN trading.order_events o ON f.order_id = o.order_id
WHERE f.date = today()
```

**Action Required**: 
Log order events and fill details
"""
    }
})
panel_id += 1

# Panel 4.2: Decision Funnel (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "Decision Funnel ⏳",
    "gridPos": {"x": 8, "y": y_pos, "w": 8, "h": 6},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for execution data

**Required**: `exec_events` table

**Query Preview**:
```sql
SELECT
  stage,
  count() as decisions
FROM trading.exec_events
WHERE date = today()
GROUP BY stage
ORDER BY stage
```

**Action Required**: 
Log execution pipeline stages
"""
    }
})
panel_id += 1

# Panel 4.3: Fill Rate (PLACEHOLDER)
dashboard["panels"].append({
    "type": "text",
    "title": "Fill Rate ⏳",
    "gridPos": {"x": 16, "y": y_pos, "w": 8, "h": 6},
    "id": panel_id,
    "options": {
        "mode": "markdown",
        "content": """
# ⏳ Placeholder

**Status**: Waiting for execution data

**Required**: `fills_live` + `order_events`

**Query Preview**:
```sql
SELECT
  countIf(status = 'filled') / count() * 100 as fill_rate_pct
FROM trading.order_events
WHERE date = today()
```

**Action Required**: 
Log order submission and fill status
"""
    }
})
panel_id += 1

# Output dashboard JSON
output_file = "./grafana/ai_boss_dashboard/ai_boss_complete.json"
with open(output_file, "w") as f:
    json.dump(dashboard, f, indent=2)

print(f"✅ Dashboard generated: {output_file}")
print(f"📊 Total panels: {len(dashboard['panels'])}")
print(f"✅ Implemented: 8 panels with real data")
print(f"🟡 Placeholders: 7 panels waiting for data")
