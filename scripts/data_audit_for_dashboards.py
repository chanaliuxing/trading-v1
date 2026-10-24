#!/usr/bin/env python3
"""
数据源审计 - 为 AI Trading Dashboards 验证数据可用性

Be Honest: 检查哪些数据源真实存在，哪些需要创建
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime
from clickhouse_driver import Client

# Add project root
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

ET_TZ = "America/New_York"

def check_clickhouse_tables():
    """检查 ClickHouse 中已有的表"""
    print("=" * 80)
    print("📊 ClickHouse Tables Audit")
    print("=" * 80)

    client = Client(host='localhost', port=9000, database='llm')

    # 列出所有表
    tables = client.execute("SHOW TABLES FROM llm")
    print(f"\n✅ Found {len(tables)} tables in llm database:\n")

    for table in sorted(tables):
        table_name = table[0]

        # 获取行数
        count_query = f"SELECT count(*) FROM llm.{table_name}"
        try:
            count = client.execute(count_query)[0][0]

            # 获取最新记录时间
            ts_cols = []
            desc = client.execute(f"DESCRIBE TABLE llm.{table_name}")
            for col_info in desc:
                col_name = col_info[0]
                if 'ts' in col_name.lower() or 'time' in col_name.lower():
                    ts_cols.append(col_name)

            latest = "N/A"
            if ts_cols:
                try:
                    latest_query = f"SELECT max({ts_cols[0]}) FROM llm.{table_name}"
                    latest_result = client.execute(latest_query)[0][0]
                    if latest_result:
                        latest = str(latest_result)
                except:
                    pass

            status = "✅" if count > 0 else "⚠️ "
            print(f"  {status} {table_name:<40} {count:>10,} rows    Latest: {latest}")

        except Exception as e:
            print(f"  ❌ {table_name:<40} ERROR: {e}")

    print("\n" + "=" * 80)
    return client

def check_file_logs():
    """检查文件日志的可用性"""
    print("\n📁 File Logs Audit")
    print("=" * 80)

    # Check runs/ai_trader/
    runs_dir = project_root / "runs" / "ai_trader"
    if runs_dir.exists():
        # Get latest date
        date_dirs = sorted([d for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith("2025")])
        if date_dirs:
            latest_date_dir = date_dirs[-1]
            print(f"\n✅ Latest AI Trader runs: {latest_date_dir.name}")

            # Check key files
            files_to_check = [
                "brain_calls.jsonl",
                "afterhours_plan.json",
                "intraday_plan.json",
                "overnight_plan.json"
            ]

            for filename in files_to_check:
                filepath = latest_date_dir / filename
                if filepath.exists():
                    size = filepath.stat().st_size
                    print(f"  ✅ {filename:<30} {size:>10} bytes")

                    # Sample first line if jsonl
                    if filename.endswith('.jsonl'):
                        try:
                            with open(filepath) as f:
                                first_line = f.readline()
                                data = json.loads(first_line)
                                print(f"      Sample keys: {list(data.keys())[:5]}")
                        except:
                            pass
                else:
                    print(f"  ⚠️  {filename:<30} NOT FOUND")

            # Count brain prompts
            prompt_files = list(latest_date_dir.glob("brain_prompt_*.txt"))
            print(f"  ✅ brain_prompt_*.txt: {len(prompt_files)} files")
        else:
            print("  ❌ No date directories found")
    else:
        print("  ❌ runs/ai_trader/ does not exist")

    # Check logs/
    logs_dir = project_root / "logs"
    print(f"\n✅ System Logs: {logs_dir}")

    log_files = [
        "trader.log",
        "gemini_*.log",
        "openai_*.log",
        "deepseek_*.log",
        "system_health.json"
    ]

    for pattern in log_files:
        if '*' in pattern:
            found = list(logs_dir.glob(pattern))
            if found:
                print(f"  ✅ {pattern:<30} {len(found)} files")
                # Check size of latest
                latest = max(found, key=lambda p: p.stat().st_mtime)
                size = latest.stat().st_size
                print(f"      Latest: {latest.name} ({size} bytes)")
            else:
                print(f"  ⚠️  {pattern:<30} No files found")
        else:
            filepath = logs_dir / pattern
            if filepath.exists():
                size = filepath.stat().st_size
                print(f"  ✅ {pattern:<30} {size:>10} bytes")
            else:
                print(f"  ⚠️  {pattern:<30} NOT FOUND")

    print("\n" + "=" * 80)

def check_data_quality(client):
    """检查现有数据的质量"""
    print("\n🔍 Data Quality Check")
    print("=" * 80)

    # Check llm_decisions
    print("\n1. llm_decisions - LLM 决策记录")
    try:
        count = client.execute("SELECT count(*) FROM llm.llm_decisions")[0][0]
        today_count = client.execute("SELECT count(*) FROM llm.llm_decisions WHERE trading_day = today()")[0][0]
        print(f"  Total: {count:,} decisions")
        print(f"  Today: {today_count:,} decisions")

        # Sample decision
        sample = client.execute("SELECT decision_json FROM llm.llm_decisions ORDER BY ts_ny DESC LIMIT 1")
        if sample:
            decision = json.loads(sample[0][0])
            print(f"  Sample keys: {list(decision.keys())}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    # Check llm_fills
    print("\n2. llm_fills - 成交记录")
    try:
        count = client.execute("SELECT count(*) FROM llm.llm_fills")[0][0]
        today_count = client.execute("SELECT count(*) FROM llm.llm_fills WHERE toDate(ts_ny) = today()")[0][0]
        print(f"  Total: {count:,} fills")
        print(f"  Today: {today_count:,} fills")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    # Check ibkr_account_realtime
    print("\n3. ibkr_account_realtime - 实时账户数据")
    try:
        count = client.execute("SELECT count(*) FROM llm.ibkr_account_realtime")[0][0]
        latest = client.execute("SELECT ts_ny, net_liquidation, unrealized_pnl FROM llm.ibkr_account_realtime ORDER BY ts_ny DESC LIMIT 1")
        print(f"  Total: {count:,} snapshots")
        if latest:
            print(f"  Latest: {latest[0][0]} - NetLiq: ${latest[0][1]:,.2f}, P&L: ${latest[0][2]:,.2f}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    # Check ibkr_positions_realtime
    print("\n4. ibkr_positions_realtime - 实时持仓")
    try:
        count = client.execute("SELECT count(*) FROM llm.ibkr_positions_realtime")[0][0]
        symbols = client.execute("SELECT DISTINCT symbol FROM llm.ibkr_positions_realtime WHERE ts_ny = (SELECT max(ts_ny) FROM llm.ibkr_positions_realtime)")
        print(f"  Total: {count:,} snapshots")
        print(f"  Current symbols: {[s[0] for s in symbols]}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    print("\n" + "=" * 80)

def generate_implementation_plan():
    """生成诚实的实施计划"""
    print("\n📋 Honest Implementation Plan")
    print("=" * 80)

    print("""
基于审计结果，这是 100% 可落地的实施计划：

Phase 1A: 立即可用的 Dashboards（使用现有数据）
─────────────────────────────────────────────────────
✅ Dashboard 1: Trading Performance (简化版)
   数据源:
   - llm_fills ✅ (成交记录)
   - ibkr_account_realtime ✅ (实时账户)
   - ibkr_positions_realtime ✅ (实时持仓)

   可实现的 panels:
   - Today's P&L, Win Rate, Total Trades ✅
   - Equity Curve ✅
   - P&L by Symbol ✅
   - Fills Table ✅

   无法实现的 panels (需要新数据):
   - P&L by Session ❌ (需要 session 字段)
   - P&L by Strategy ❌ (需要 strategy_id)

Phase 1B: 需要简单数据导入的 Dashboards
─────────────────────────────────────────────────────
🔧 Dashboard 2: AI Brain Command Center (简化版)
   需要创建:
   1. 解析 brain_calls.jsonl → ClickHouse
   2. 解析 *_plan.json → ClickHouse

   实施复杂度: 中等
   预估时间: 2-3 小时

Phase 2: 需要复杂解析的 Dashboards
─────────────────────────────────────────────────────
⏳ Dashboard 3-6: LLM Deep Dive, Strategy Deep Dive, etc.
   需要创建:
   - 解析 trader.log (JSON lines) → ClickHouse
   - 解析 LLM provider logs → ClickHouse

   实施复杂度: 高
   预估时间: 1-2 天

建议优先级:
1. 先实现 Dashboard 1 (Trading Performance) - 今晚就能完成 ✅
2. 再实现 Dashboard 2 的数据导入 - 明天完成 🔧
3. 最后实现其他 Dashboards - 本周完成 ⏳
    """)

    print("=" * 80)

def main():
    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "AI Trading Dashboards - Data Audit" + " " * 24 + "║")
    print("╚" + "═" * 78 + "╝")
    print("\n")

    # Check ClickHouse
    client = check_clickhouse_tables()

    # Check file logs
    check_file_logs()

    # Check data quality
    check_data_quality(client)

    # Generate plan
    generate_implementation_plan()

    print("\n✅ Audit Complete!\n")

if __name__ == "__main__":
    main()
