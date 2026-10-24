#!/usr/bin/env python3
"""
生成 Dashboard 4: LLM Sessions Monitor

基于证据的设计:
- llm_sessions: 574 records ✅
- llm_equity_timeseries: 613 records ✅
- llm_decisions: 464 records ✅
- llm_order_events: 14 records ⚠️ (少但可用)
"""

import json
from datetime import datetime

def generate_dashboard():
    dashboard = {
        "title": "LLM Sessions Monitor",
        "uid": "llm-sessions-monitor",
        "tags": ["llm", "sessions", "trading"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        "refresh": "30s",
        "time": {
            "from": "now-7d",
            "to": "now"
        },
        "panels": [],
        "templating": {
            "list": []
        }
    }

    panel_id = 1
    y_pos = 0

    # ============================================================================
    # Row 1: Sessions Overview (4 panels)
    # ============================================================================
    dashboard["panels"].append({
        "type": "row",
        "title": "📊 Sessions Overview (会话概览)",
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y_pos},
        "id": panel_id,
        "collapsed": False
    })
    panel_id += 1
    y_pos += 1

    # Panel 1.1: Total Sessions (总会话数)
    dashboard["panels"].append({
        "type": "stat",
        "title": "📊 Total Sessions (总会话数)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT count(DISTINCT session_id) as total_sessions
FROM llm.llm_sessions
WHERE start_ts_ny >= now() - INTERVAL 7 DAY
"""
        }],
        "gridPos": {"h": 4, "w": 6, "x": 0, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "color": {"mode": "palette-classic"}
            }
        }
    })
    panel_id += 1

    # Panel 1.2: Running Sessions (运行中会话)
    dashboard["panels"].append({
        "type": "stat",
        "title": "🔄 Running Sessions (运行中)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT count(*) as running_sessions
FROM llm.llm_sessions
WHERE status = 'RUNNING'
  AND start_ts_ny >= now() - INTERVAL 7 DAY
"""
        }],
        "gridPos": {"h": 4, "w": 4, "x": 6, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"value": 0, "color": "red"},
                        {"value": 1, "color": "green"}
                    ]
                }
            }
        }
    })
    panel_id += 1

    # Panel 1.2b: Zombie Sessions (僵尸会话) - CRITICAL FIX
    dashboard["panels"].append({
        "type": "stat",
        "title": "⚠️ Zombie Sessions (僵尸会话 >24h)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT count(*) as zombie_sessions
FROM llm.llm_sessions
WHERE status = 'RUNNING'
  AND (now() - start_ts_ny) > 86400  -- 24 hours = 86400 seconds
"""
        }],
        "gridPos": {"h": 4, "w": 4, "x": 10, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"value": 0, "color": "green"},      # 0 zombies - OK
                        {"value": 1, "color": "yellow"},     # 1-9 zombies - WARNING
                        {"value": 10, "color": "orange"},    # 10-49 zombies - SEVERE
                        {"value": 50, "color": "red"}        # 50+ zombies - CRITICAL
                    ]
                }
            }
        },
        "options": {
            "colorMode": "background"
        }
    })
    panel_id += 1

    # Panel 1.3: Bot Types (Bot 类型数)
    dashboard["panels"].append({
        "type": "stat",
        "title": "🤖 Bot Types (Bot 类型)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT count(DISTINCT bot_id) as bot_types
FROM llm.llm_sessions
WHERE start_ts_ny >= now() - INTERVAL 7 DAY
"""
        }],
        "gridPos": {"h": 4, "w": 5, "x": 14, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "color": {"mode": "palette-classic"}
            }
        }
    })
    panel_id += 1

    # Panel 1.4: Decisions Made (决策总数)
    dashboard["panels"].append({
        "type": "stat",
        "title": "🎯 Decisions Made (决策总数)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT count(*) as total_decisions
FROM llm.llm_decisions
WHERE ts_ny >= now() - INTERVAL 7 DAY
"""
        }],
        "gridPos": {"h": 4, "w": 5, "x": 19, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "color": {"mode": "palette-classic"}
            }
        }
    })
    panel_id += 1
    y_pos += 4

    # ============================================================================
    # Row 2: Sessions Timeline (2 panels)
    # ============================================================================
    dashboard["panels"].append({
        "type": "row",
        "title": "📈 Sessions Timeline (会话时间线)",
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y_pos},
        "id": panel_id,
        "collapsed": False
    })
    panel_id += 1
    y_pos += 1

    # Panel 2.1: Sessions Over Time (会话启动趋势)
    dashboard["panels"].append({
        "type": "timeseries",
        "title": "📊 Sessions Started Over Time (会话启动趋势)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    toStartOfHour(start_ts_ny) as time,
    count(*) as sessions_started
FROM llm.llm_sessions
WHERE start_ts_ny >= now() - INTERVAL 7 DAY
GROUP BY time
ORDER BY time
"""
        }],
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 20,
                    "showPoints": "auto"
                },
                "color": {"mode": "palette-classic"}
            }
        }
    })
    panel_id += 1

    # Panel 2.2: Active Sessions Count (活跃会话数)
    dashboard["panels"].append({
        "type": "timeseries",
        "title": "🔄 Active Sessions Count (活跃会话数)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    toStartOfHour(start_ts_ny) as time,
    status,
    count(*) as session_count
FROM llm.llm_sessions
WHERE start_ts_ny >= now() - INTERVAL 7 DAY
GROUP BY time, status
ORDER BY time
"""
        }],
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 10,
                    "showPoints": "auto",
                    "stacking": {"mode": "normal"}
                }
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "RUNNING session_count"},
                    "properties": [{"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}]
                }
            ]
        }
    })
    panel_id += 1
    y_pos += 8

    # ============================================================================
    # Row 3: Bot & Model Distribution (3 panels)
    # ============================================================================
    dashboard["panels"].append({
        "type": "row",
        "title": "🤖 Bot & Model Distribution (Bot 和模型分布)",
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y_pos},
        "id": panel_id,
        "collapsed": False
    })
    panel_id += 1
    y_pos += 1

    # Panel 3.1: Sessions by Bot (按 Bot 分布)
    dashboard["panels"].append({
        "type": "piechart",
        "title": "🤖 Sessions by Bot (按 Bot 分布)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    bot_id,
    count(*) as sessions
FROM llm.llm_sessions
WHERE start_ts_ny >= now() - INTERVAL 7 DAY
GROUP BY bot_id
ORDER BY sessions DESC
"""
        }],
        "gridPos": {"h": 8, "w": 8, "x": 0, "y": y_pos},
        "id": panel_id,
        "options": {
            "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"]}
        }
    })
    panel_id += 1

    # Panel 3.2: Sessions by Model (按模型分布)
    dashboard["panels"].append({
        "type": "piechart",
        "title": "📊 Sessions by Model (按模型分布)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    model,
    count(*) as sessions
FROM llm.llm_sessions
WHERE start_ts_ny >= now() - INTERVAL 7 DAY
GROUP BY model
ORDER BY sessions DESC
"""
        }],
        "gridPos": {"h": 8, "w": 8, "x": 8, "y": y_pos},
        "id": panel_id,
        "options": {
            "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"]}
        }
    })
    panel_id += 1

    # Panel 3.3: Sessions by Mode (按模式分布)
    dashboard["panels"].append({
        "type": "barchart",
        "title": "📈 Sessions by Mode (按模式分布)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    mode,
    count(*) as sessions
FROM llm.llm_sessions
WHERE start_ts_ny >= now() - INTERVAL 7 DAY
GROUP BY mode
ORDER BY sessions DESC
"""
        }],
        "gridPos": {"h": 8, "w": 8, "x": 16, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "color": {"mode": "palette-classic"}
            }
        },
        "options": {
            "orientation": "horizontal"
        }
    })
    panel_id += 1
    y_pos += 8

    # ============================================================================
    # Row 4: Trading Activity (2 panels)
    # ============================================================================
    dashboard["panels"].append({
        "type": "row",
        "title": "💼 Trading Activity (交易活动)",
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y_pos},
        "id": panel_id,
        "collapsed": False
    })
    panel_id += 1
    y_pos += 1

    # Panel 4.1: Decisions by Action (按决策类型分布)
    dashboard["panels"].append({
        "type": "piechart",
        "title": "🎯 Decisions by Action (按决策类型)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    JSONExtractString(decision_json, 'action') as action,
    count(*) as count
FROM llm.llm_decisions
WHERE ts_ny >= now() - INTERVAL 7 DAY
GROUP BY action
ORDER BY count DESC
"""
        }],
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": y_pos},
        "id": panel_id,
        "options": {
            "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"]}
        }
    })
    panel_id += 1

    # Panel 4.2: Decisions Over Time (决策时间线)
    dashboard["panels"].append({
        "type": "timeseries",
        "title": "📊 Decisions Over Time (决策时间线)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    toStartOfHour(ts_ny) as time,
    JSONExtractString(decision_json, 'action') as action,
    count(*) as count
FROM llm.llm_decisions
WHERE ts_ny >= now() - INTERVAL 7 DAY
GROUP BY time, action
ORDER BY time
"""
        }],
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 10,
                    "stacking": {"mode": "normal"}
                }
            }
        }
    })
    panel_id += 1
    y_pos += 8

    # ============================================================================
    # Row 5: Recent Sessions (1 panel)
    # ============================================================================
    dashboard["panels"].append({
        "type": "row",
        "title": "📋 Recent Sessions (最近会话)",
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y_pos},
        "id": panel_id,
        "collapsed": False
    })
    panel_id += 1
    y_pos += 1

    # Panel 5.0: Zombie Sessions Table (僵尸会话表) - CRITICAL FIX
    dashboard["panels"].append({
        "type": "table",
        "title": "⚠️ Zombie Sessions (僵尸会话 - RUNNING >24h)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    formatDateTime(start_ts_ny, '%Y-%m-%d %H:%M:%S ET') as start_time,
    round((now() - start_ts_ny) / 3600, 1) as hours_running,
    substring(session_id, 1, 12) as session_id,
    bot_id,
    model,
    mode,
    symbols,
    status
FROM llm.llm_sessions
WHERE status = 'RUNNING'
  AND (now() - start_ts_ny) > 86400  -- 24 hours
ORDER BY start_ts_ny ASC
LIMIT 100
"""
        }],
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "hours_running"},
                    "properties": [
                        {"id": "custom.width", "value": 130},
                        {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                        {"id": "thresholds", "value": {
                            "mode": "absolute",
                            "steps": [
                                {"value": 24, "color": "yellow"},
                                {"value": 48, "color": "orange"},
                                {"value": 168, "color": "red"}
                            ]
                        }}
                    ]
                },
                {"matcher": {"id": "byName", "options": "status"}, "properties": [{"id": "custom.width", "value": 100}]},
                {"matcher": {"id": "byName", "options": "mode"}, "properties": [{"id": "custom.width", "value": 80}]}
            ]
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "hours_running", "desc": True}]
        }
    })
    panel_id += 1
    y_pos += 8

    # Panel 5.1: Sessions Table (会话表)
    dashboard["panels"].append({
        "type": "table",
        "title": "📋 Recent Sessions Table (最近会话列表)",
        "datasource": {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"},
        "targets": [{
            "refId": "A",
            "rawSql": """
SELECT
    formatDateTime(start_ts_ny, '%Y-%m-%d %H:%M:%S') as start_time,
    substring(session_id, 1, 8) as session_short,
    bot_id,
    model,
    symbols,
    mode,
    status,
    close_reason
FROM llm.llm_sessions
WHERE start_ts_ny >= now() - INTERVAL 7 DAY
ORDER BY start_ts_ny DESC
LIMIT 50
"""
        }],
        "gridPos": {"h": 10, "w": 24, "x": 0, "y": y_pos},
        "id": panel_id,
        "fieldConfig": {
            "overrides": [
                {"matcher": {"id": "byName", "options": "status"}, "properties": [{"id": "custom.width", "value": 100}]},
                {"matcher": {"id": "byName", "options": "mode"}, "properties": [{"id": "custom.width", "value": 80}]}
            ]
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "start_time", "desc": True}]
        }
    })
    panel_id += 1
    y_pos += 10

    print("=" * 80)
    print("✅ LLM Sessions Monitor Dashboard 已生成!")
    print("=" * 80)

    output_file = "./grafana/dashboards/active/llm_sessions_monitor.json"
    with open(output_file, 'w') as f:
        json.dump(dashboard, f, indent=2)

    print(f"📍 位置: {output_file}\n")
    print("📊 Dashboard 结构:\n")
    print("   Row 1: 📊 Sessions Overview (4 panels)")
    print("      - Total Sessions (总会话数)")
    print("      - Running Sessions (运行中)")
    print("      - Bot Types (Bot 类型数)")
    print("      - Decisions Made (决策总数)\n")

    print("   Row 2: 📈 Sessions Timeline (2 panels)")
    print("      - Sessions Started Over Time (会话启动趋势)")
    print("      - Active Sessions Count (活跃会话数)\n")

    print("   Row 3: 🤖 Bot & Model Distribution (3 panels)")
    print("      - Sessions by Bot (按 Bot 分布)")
    print("      - Sessions by Model (按模型分布)")
    print("      - Sessions by Mode (按模式分布)\n")

    print("   Row 4: 💼 Trading Activity (2 panels)")
    print("      - Decisions by Action (按决策类型)")
    print("      - Decisions Over Time (决策时间线)\n")

    print("   Row 5: 📋 Recent Sessions (1 panel)")
    print("      - Sessions Table (最近会话列表)\n")

    print("=" * 80)
    print(f"✅ 总计: 12 data panels + 5 row headers = 17 components")
    print("=" * 80)
    print("\n📊 数据源:")
    print("   - llm_sessions: 574 records ✅")
    print("   - llm_decisions: 464 records ✅\n")
    print("🌐 访问: http://localhost:3000/d/llm-sessions-monitor\n")
    print("✅ 100% 基于真实数据！")

if __name__ == "__main__":
    generate_dashboard()
