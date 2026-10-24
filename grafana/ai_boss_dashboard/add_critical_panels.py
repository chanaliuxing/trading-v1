#!/usr/bin/env python3
"""
Add CRITICAL panels to AI Boss Dashboard:
1. Total Equity (总资金) - MOST IMPORTANT
2. Session PnL breakdown (分时段盈亏)
"""

import json

# Read existing dashboard
with open("./grafana/dashboards/active/ai_boss_complete.json", "r") as f:
    dashboard = json.load(f)

# Find Row 1 and insert new critical panels at the beginning
row1_idx = None
for i, panel in enumerate(dashboard["panels"]):
    if panel.get("type") == "row" and "Executive" in panel.get("title", ""):
        row1_idx = i
        break

if row1_idx is None:
    print("Error: Row 1 not found")
    exit(1)

# Get max panel ID
max_id = max(p.get("id", 0) for p in dashboard["panels"])

# Shift existing Row 1 panels to make room
# Row 1 currently starts at y=1, we'll insert at y=1, shift others down
for panel in dashboard["panels"]:
    if panel.get("gridPos", {}).get("y", 0) >= 1:
        panel["gridPos"]["y"] += 8  # Make room for 2 rows of new panels

# Update Row 1 y position
dashboard["panels"][row1_idx]["gridPos"]["y"] = 0

#==============================================================================
# INSERT CRITICAL PANELS
#==============================================================================

critical_panels = []
panel_id = max_id + 1

# Panel: Total Equity - THE MOST IMPORTANT!
critical_panels.append({
    "type": "stat",
    "title": "💰 Total Equity (总资金) ⚠️ CRITICAL",
    "gridPos": {"x": 0, "y": 1, "w": 8, "h": 4},
    "id": panel_id,
    "datasource": {"type": "grafana-clickhouse-datasource", "uid": "ch"},
    "targets": [{
        "refId": "A",
        "rawSql": """
-- ⚠️ PLACEHOLDER: Need live trading data
-- This query will work once equity_pnl_snapshots is populated

SELECT 
  anyLast(equity) as total_equity
FROM trading.equity_pnl_snapshots
WHERE date = today()

-- ALTERNATIVE (if no data): Show message
-- Uncomment below and comment above when no data
/*
SELECT 0 as total_equity
*/
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
                    {"value": 100000, "color": "yellow"},
                    {"value": 500000, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "graphMode": "area",
        "colorMode": "background",
        "textMode": "value",
        "reduceOptions": {
            "calcs": ["lastNotNull"]
        }
    },
    "description": """
⚠️ **CRITICAL METRIC - NEEDS DATA**

**Status**: Placeholder - Waiting for live trading

**Required**: Log equity snapshots to `equity_pnl_snapshots` table

**Action**: 
1. In account monitor, log equity every minute:
   ```python
   INSERT INTO trading.equity_pnl_snapshots 
   (date, ts, equity, realized_pnl, unrealized_pnl, cash)
   VALUES (today(), now(), ?, ?, ?, ?)
   ```

2. Once data flows, this panel will show total account value

**Priority**: P0 - HIGHEST
"""
})
panel_id += 1

# Panel: Equity Chart (24h)
critical_panels.append({
    "type": "timeseries",
    "title": "Equity Curve (24h)",
    "gridPos": {"x": 8, "y": 1, "w": 16, "h": 4},
    "id": panel_id,
    "datasource": {"type": "grafana-clickhouse-datasource", "uid": "ch"},
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
                "fillOpacity": 20,
                "showPoints": "never"
            },
            "unit": "currencyUSD",
            "decimals": 2
        }
    },
    "description": "⚠️ Placeholder - needs equity_pnl_snapshots data"
})
panel_id += 1

# Panel: Session PnL Breakdown
critical_panels.append({
    "type": "stat",
    "title": "📊 Session PnL (分时段盈亏) ⚠️",
    "gridPos": {"x": 0, "y": 5, "w": 24, "h": 4},
    "id": panel_id,
    "datasource": {"type": "grafana-clickhouse-datasource", "uid": "ch"},
    "targets": [
        {
            "refId": "A",
            "rawSql": """
-- Premarket (04:00-09:30)
SELECT 
  'Premarket' as session,
  sum(pnl) as session_pnl
FROM trading.session_pnl
WHERE date = today()
  AND session = 'premarket'
"""
        },
        {
            "refId": "B", 
            "rawSql": """
-- Intraday (09:30-16:00)
SELECT 
  'Intraday' as session,
  sum(pnl) as session_pnl
FROM trading.session_pnl
WHERE date = today()
  AND session = 'intraday'
"""
        },
        {
            "refId": "C",
            "rawSql": """
-- Afterhours (16:00-20:00)
SELECT 
  'Afterhours' as session,
  sum(pnl) as session_pnl
FROM trading.session_pnl
WHERE date = today()
  AND session = 'afterhours'
"""
        },
        {
            "refId": "D",
            "rawSql": """
-- Overnight (20:00-04:00)
SELECT 
  'Overnight' as session,
  sum(pnl) as session_pnl
FROM trading.session_pnl
WHERE date = today()
  AND session = 'overnight'
"""
        }
    ],
    "fieldConfig": {
        "defaults": {
            "unit": "currencyUSD",
            "decimals": 2,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"value": -1000, "color": "red"},
                    {"value": 0, "color": "yellow"},
                    {"value": 1000, "color": "green"}
                ]
            }
        }
    },
    "options": {
        "graphMode": "none",
        "colorMode": "background",
        "textMode": "value_and_name",
        "orientation": "horizontal"
    },
    "description": """
⚠️ **CRITICAL METRIC - NEEDS DATA**

**Status**: Placeholder - Waiting for session PnL tracking

**Required**: Create `session_pnl` table and log PnL by session

**Schema**:
```sql
CREATE TABLE trading.session_pnl (
    date Date,
    ts DateTime,
    session Enum8('premarket'=1, 'intraday'=2, 'afterhours'=3, 'overnight'=4),
    pnl Float64,
    trades UInt32
) ENGINE = MergeTree()
ORDER BY (date, session, ts);
```

**Session Definitions**:
- **Premarket** (盘前): 04:00 - 09:30 EST
- **Intraday** (盘中): 09:30 - 16:00 EST  
- **Afterhours** (盘后): 16:00 - 20:00 EST
- **Overnight** (隔夜): 20:00 - 04:00 EST

**Action**: 
1. Create session_pnl table
2. Log PnL at end of each session
3. Or calculate from trades: GROUP BY session based on timestamp

**Priority**: P0 - HIGHEST
"""
})
panel_id += 1

# Insert critical panels after Row 1 title
insert_position = row1_idx + 1
for panel in reversed(critical_panels):
    dashboard["panels"].insert(insert_position, panel)

# Save updated dashboard
with open("./grafana/dashboards/active/ai_boss_complete.json", "w") as f:
    json.dump(dashboard, f, indent=2)

print("✅ Critical panels added to dashboard!")
print(f"📊 Added {len(critical_panels)} critical panels:")
print("   1. Total Equity (总资金) - STAT + CHART")
print("   2. Session PnL (分时段盈亏) - 4-way STAT")
print("")
print("⚠️ These panels are PLACEHOLDERS - need live trading data")
print("")
print("🔧 Next steps:")
print("   1. Implement equity_pnl_snapshots logging")
print("   2. Implement session_pnl tracking")
print("   3. See PLACEHOLDER_IMPLEMENTATION_GUIDE.md for details")
