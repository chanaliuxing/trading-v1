#!/usr/bin/env python3
"""
导入 AI Brain 日志到 ClickHouse

数据源:
- runs/ai_trader/{date}/brain_calls.jsonl → llm.ai_brain_calls
- runs/ai_trader/{date}/*_plan.json → llm.ai_brain_plans

Be Honest: 处理缺失文件，跳过无效数据
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from clickhouse_driver import Client

# Add project root
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

ET_TZ = ZoneInfo("America/New_York")

def parse_timestamp(ts_str):
    """解析时间戳，支持多种格式"""
    # Try parsing with timezone
    try:
        if 'T' in ts_str and ('+' in ts_str or '-' in ts_str[-6:]):
            # Format: 2025-11-01T01:43:27.488362-04:00
            dt = datetime.fromisoformat(ts_str)
            return dt.astimezone(ET_TZ)
        else:
            # Format: 2025-11-01T00:03:50.813086 (no timezone)
            dt = datetime.fromisoformat(ts_str)
            # Assume ET
            return dt.replace(tzinfo=ET_TZ)
    except Exception as e:
        print(f"  ⚠️  Failed to parse timestamp: {ts_str} - {e}")
        return None

def ingest_brain_calls(client, date_str):
    """导入 brain_calls.jsonl"""
    print(f"\n📥 Ingesting brain_calls for {date_str}...")

    calls_file = project_root / "runs" / "ai_trader" / date_str / "brain_calls.jsonl"

    if not calls_file.exists():
        print(f"  ⚠️  File not found: {calls_file}")
        return 0

    # Parse JSONL
    records = []
    with open(calls_file) as f:
        for line_num, line in enumerate(f, 1):
            try:
                data = json.loads(line.strip())

                # Parse timestamp
                ts = parse_timestamp(data['ts'])
                if not ts:
                    continue

                # Extract fields
                record = {
                    'ts_ny': ts,
                    'date': ts.date(),
                    'kind': data.get('kind', ''),
                    'session': data.get('session', ''),
                    'provider': data.get('provider', ''),
                    'model': data.get('model', ''),
                    'input_keys': data.get('input_keys', []),
                    'output_json': json.dumps(data.get('output', {})),
                    'duration_ms': data.get('duration_ms', 0),
                    'trading_universe_count': data.get('trading_universe_count', 0)
                }

                records.append(record)

            except Exception as e:
                print(f"  ⚠️  Line {line_num} failed: {e}")
                continue

    if not records:
        print("  ⚠️  No valid records found")
        return 0

    # Insert to ClickHouse
    query = """
        INSERT INTO llm.ai_brain_calls
        (ts_ny, date, kind, session, provider, model, input_keys, output_json, duration_ms, trading_universe_count)
        VALUES
    """

    values = [
        (
            r['ts_ny'],
            r['date'],
            r['kind'],
            r['session'],
            r['provider'],
            r['model'],
            r['input_keys'],
            r['output_json'],
            r['duration_ms'],
            r['trading_universe_count']
        )
        for r in records
    ]

    try:
        client.execute(query, values)
        print(f"  ✅ Inserted {len(records)} brain_calls")
        return len(records)
    except Exception as e:
        print(f"  ❌ Insert failed: {e}")
        return 0

def ingest_brain_plans(client, date_str):
    """导入 *_plan.json"""
    print(f"\n📥 Ingesting brain_plans for {date_str}...")

    plans_dir = project_root / "runs" / "ai_trader" / date_str
    if not plans_dir.exists():
        print(f"  ⚠️  Directory not found: {plans_dir}")
        return 0

    # Find all *_plan.json files
    plan_files = list(plans_dir.glob("*_plan.json"))
    if not plan_files:
        print("  ⚠️  No plan files found")
        return 0

    print(f"  Found {len(plan_files)} plan files")

    records = []
    for plan_file in plan_files:
        try:
            with open(plan_file) as f:
                data = json.load(f)

            # Parse timestamp
            ts = parse_timestamp(data.get('ts') or data.get('generated_at', ''))
            if not ts:
                print(f"  ⚠️  Skipping {plan_file.name}: no valid timestamp")
                continue

            # Extract session from tag
            session = data.get('tag', '')

            # Extract executed_plan
            detail = data.get('detail', {})
            status = detail.get('status', 'unknown')
            executed_plan = detail.get('executed_plan', {})

            # Extract providers and symbols
            providers = []
            symbols = []
            for prov in executed_plan.get('providers', []):
                providers.append(prov.get('name', ''))
                symbols.extend(prov.get('symbols', []))

            # Deduplicate symbols
            symbols = list(set(symbols))

            # Extract AB ratios
            ab = executed_plan.get('ab', {})
            ab_champion = ab.get('champion', 0.5)
            ab_challenger = ab.get('challenger', 0.5)

            # Extract flags
            safe_mode = executed_plan.get('safe_mode', False)
            restrictions = executed_plan.get('restrictions', [])
            reasoning_profile = executed_plan.get('reasoning_profile', '')
            notes = executed_plan.get('notes', '')

            record = {
                'ts_ny': ts,
                'date': ts.date(),
                'session': session,
                'status': status,
                'providers': providers,
                'symbols': symbols,
                'safe_mode': safe_mode,
                'restrictions': restrictions,
                'ab_champion': ab_champion,
                'ab_challenger': ab_challenger,
                'reasoning_profile': reasoning_profile,
                'notes': notes,
                'plan_json': json.dumps(executed_plan)
            }

            records.append(record)

        except Exception as e:
            print(f"  ⚠️  Failed to parse {plan_file.name}: {e}")
            continue

    if not records:
        print("  ⚠️  No valid records found")
        return 0

    # Insert to ClickHouse
    query = """
        INSERT INTO llm.ai_brain_plans
        (ts_ny, date, session, status, providers, symbols, safe_mode, restrictions,
         ab_champion, ab_challenger, reasoning_profile, notes, plan_json)
        VALUES
    """

    values = [
        (
            r['ts_ny'],
            r['date'],
            r['session'],
            r['status'],
            r['providers'],
            r['symbols'],
            r['safe_mode'],
            r['restrictions'],
            r['ab_champion'],
            r['ab_challenger'],
            r['reasoning_profile'],
            r['notes'],
            r['plan_json']
        )
        for r in records
    ]

    try:
        client.execute(query, values)
        print(f"  ✅ Inserted {len(records)} brain_plans")
        return len(records)
    except Exception as e:
        print(f"  ❌ Insert failed: {e}")
        return 0

def main():
    import argparse

    parser = argparse.ArgumentParser(description='Ingest AI Brain logs to ClickHouse')
    parser.add_argument('--date', help='Date (YYYY-MM-DD), default: today')
    parser.add_argument('--days', type=int, default=1, help='Number of days to process')
    parser.add_argument('--all', action='store_true', help='Process all available dates')
    args = parser.parse_args()

    print("=" * 80)
    print("📥 AI Brain Logs Ingestion")
    print("=" * 80)

    # Connect to ClickHouse
    client = Client(host='localhost', port=9000, database='llm')

    # Determine dates to process
    if args.all:
        # Find all date directories
        runs_dir = project_root / "runs" / "ai_trader"
        date_dirs = sorted([d.name for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith("2025")])
        print(f"\n📅 Processing {len(date_dirs)} dates: {date_dirs[0]} to {date_dirs[-1]}")
    elif args.date:
        date_dirs = [args.date]
    else:
        # Default: today
        today = date.today()
        date_dirs = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.days)]

    # Process each date
    total_calls = 0
    total_plans = 0

    for date_str in date_dirs:
        print(f"\n{'=' * 80}")
        print(f"📅 Processing {date_str}")
        print("=" * 80)

        calls_count = ingest_brain_calls(client, date_str)
        plans_count = ingest_brain_plans(client, date_str)

        total_calls += calls_count
        total_plans += plans_count

    # Summary
    print("\n" + "=" * 80)
    print("📊 Summary")
    print("=" * 80)
    print(f"  Total brain_calls: {total_calls}")
    print(f"  Total brain_plans: {total_plans}")

    # Verify data in ClickHouse
    print("\n🔍 Verification:")
    calls_count_db = client.execute("SELECT count(*) FROM llm.ai_brain_calls")[0][0]
    plans_count_db = client.execute("SELECT count(*) FROM llm.ai_brain_plans")[0][0]
    print(f"  ai_brain_calls in DB: {calls_count_db}")
    print(f"  ai_brain_plans in DB: {plans_count_db}")

    print("\n✅ Ingestion complete!")
    print("=" * 80)

if __name__ == "__main__":
    main()
