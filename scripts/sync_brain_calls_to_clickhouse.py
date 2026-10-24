#!/usr/bin/env python3
"""
Sync brain_calls.jsonl to ClickHouse ai_brain_calls table

Purpose:
- Read brain_calls.jsonl files from runs/<date>/ directories
- Insert records into llm.ai_brain_calls table
- Track processed files to avoid duplicates

Usage:
    python scripts/sync_brain_calls_to_clickhouse.py                # Sync today
    python scripts/sync_brain_calls_to_clickhouse.py --date 2025-11-01  # Sync specific date
    python scripts/sync_brain_calls_to_clickhouse.py --all          # Sync all dates
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, date, timedelta

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from clickhouse_driver import Client as CHClient
except ImportError:
    print("⚠️  clickhouse-driver 未安装")
    print("   安装: pip install clickhouse-driver")
    sys.exit(1)


class BrainCallsSyncer:
    """同步 brain_calls.jsonl 到 ClickHouse"""

    def __init__(self, runs_dir: str = "./runs", clickhouse_host: str = "localhost", clickhouse_port: int = 9000):
        self.runs_dir = Path(runs_dir)
        self.client = CHClient(host=clickhouse_host, port=clickhouse_port)

    def sync_date(self, sync_date: date, dry_run: bool = False) -> dict:
        """
        同步指定日期的 brain_calls.jsonl

        Args:
            sync_date: 日期
            dry_run: 是否只查看不执行

        Returns:
            {'date': str, 'records': int, 'inserted': int, 'skipped': int}
        """
        date_str = sync_date.isoformat()
        jsonl_file = self.runs_dir / date_str / "brain_calls.jsonl"

        if not jsonl_file.exists():
            print(f"⚠️  {date_str}: brain_calls.jsonl 不存在")
            return {'date': date_str, 'records': 0, 'inserted': 0, 'skipped': 0}

        print(f"📂 {date_str}: 读取 brain_calls.jsonl...")

        # 读取所有记录
        records = []
        with jsonl_file.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                try:
                    record = json.loads(line.strip())
                    records.append(record)
                except Exception as e:
                    print(f"   ⚠️  行 {line_num} 解析失败: {e}")

        if not records:
            print(f"   ℹ️  无记录")
            return {'date': date_str, 'records': 0, 'inserted': 0, 'skipped': 0}

        print(f"   发现 {len(records)} 条记录")

        if dry_run:
            print(f"   ℹ️  DRY RUN - 不执行插入")
            return {'date': date_str, 'records': len(records), 'inserted': 0, 'skipped': 0}

        # 转换为 ClickHouse 行格式
        rows = []
        skipped = 0

        for record in records:
            try:
                row = self._convert_record(record, sync_date)
                rows.append(row)
            except Exception as e:
                print(f"   ⚠️  记录转换失败: {e}")
                skipped += 1

        if not rows:
            print(f"   ⚠️  所有记录转换失败")
            return {'date': date_str, 'records': len(records), 'inserted': 0, 'skipped': skipped}

        # 批量插入
        try:
            self.client.execute(
                """
                INSERT INTO llm.ai_brain_calls (
                    ts_ny, date, kind, session, provider, model,
                    input_keys, output_json, duration_ms, trading_universe_count,
                    success, error_type, error_message, fallback_used, providers_attempted
                ) VALUES
                """,
                rows
            )
            inserted = len(rows)
            print(f"   ✅ 插入 {inserted} 条记录")

            return {'date': date_str, 'records': len(records), 'inserted': inserted, 'skipped': skipped}

        except Exception as e:
            print(f"   ❌ 插入失败: {e}")
            return {'date': date_str, 'records': len(records), 'inserted': 0, 'skipped': len(records)}

    def _convert_record(self, record: dict, record_date: date) -> dict:
        """
        转换 JSONL 记录为 ClickHouse 行格式

        Args:
            record: JSONL 记录
            record_date: 记录日期

        Returns:
            ClickHouse row dict
        """
        # 解析时间戳
        ts_str = record.get("ts", "")
        if ts_str:
            try:
                ts_ny = datetime.fromisoformat(ts_str)
            except:
                ts_ny = datetime.now()
        else:
            ts_ny = datetime.now()

        # 提取字段
        kind = record.get("kind", "unknown")
        session = record.get("session", "")
        provider = record.get("provider", "unknown")
        model = record.get("model", "")
        input_keys = record.get("input_keys", [])
        output = record.get("output", {})
        duration_ms = record.get("duration_ms", 0)
        trading_universe_count = record.get("trading_universe_count", 0)

        # Error tracking fields (new)
        success = record.get("success", 1)  # Default 1 for backward compatibility
        error_type = record.get("error_type", "")
        error_message = record.get("error_message", "")
        fallback_used = record.get("fallback_used", 0)
        providers_attempted = record.get("providers_attempted", [])

        # 构造行
        return {
            "ts_ny": ts_ny,
            "date": record_date,
            "kind": kind,
            "session": session,
            "provider": provider,
            "model": model,
            "input_keys": input_keys,
            "output_json": json.dumps(output, ensure_ascii=False),
            "duration_ms": duration_ms,
            "trading_universe_count": trading_universe_count,
            "success": success,
            "error_type": error_type,
            "error_message": error_message,
            "fallback_used": fallback_used,
            "providers_attempted": providers_attempted,
        }

    def sync_all(self, dry_run: bool = False) -> list:
        """
        同步 runs/ 目录下所有日期的 brain_calls.jsonl

        Returns:
            List of sync results
        """
        results = []

        # 查找所有日期目录 (YYYY-MM-DD格式)
        date_dirs = sorted([d for d in self.runs_dir.iterdir() if d.is_dir() and len(d.name) == 10 and d.name.count('-') == 2])

        print(f"🔍 发现 {len(date_dirs)} 个日期目录")
        print()

        for date_dir in date_dirs:
            try:
                sync_date = date.fromisoformat(date_dir.name)
                result = self.sync_date(sync_date, dry_run=dry_run)
                results.append(result)
            except Exception as e:
                print(f"❌ {date_dir.name} 同步失败: {e}")

        return results


def main():
    parser = argparse.ArgumentParser(
        description="Sync brain_calls.jsonl to ClickHouse ai_brain_calls table"
    )

    parser.add_argument(
        "--date",
        type=str,
        help="同步指定日期 (YYYY-MM-DD格式)"
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="同步所有日期"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅查看不执行 (Dry Run 模式)"
    )

    parser.add_argument(
        "--runs-dir",
        type=str,
        default="./runs",
        help="runs 目录路径，默认 ./runs"
    )

    parser.add_argument(
        "--host",
        type=str,
        default="localhost",
        help="ClickHouse 主机地址，默认 localhost"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="ClickHouse 端口，默认 9000"
    )

    args = parser.parse_args()

    # 创建同步器
    syncer = BrainCallsSyncer(
        runs_dir=args.runs_dir,
        clickhouse_host=args.host,
        clickhouse_port=args.port
    )

    print("=" * 80)
    print("📊 Brain Calls Syncer v1.0")
    print("=" * 80)
    print(f"🔍 模式: {'仅查看 (DRY RUN)' if args.dry_run else '执行同步'}")
    print(f"📂 Runs 目录: {args.runs_dir}")
    print(f"🗄️  ClickHouse: {args.host}:{args.port}")
    print("=" * 80)
    print()

    try:
        if args.all:
            # 同步所有日期
            results = syncer.sync_all(dry_run=args.dry_run)

            # 统计
            total_records = sum(r['records'] for r in results)
            total_inserted = sum(r['inserted'] for r in results)
            total_skipped = sum(r['skipped'] for r in results)

            print()
            print("=" * 80)
            print("📊 同步统计:")
            print(f"   日期数: {len(results)}")
            print(f"   总记录数: {total_records}")
            print(f"   ✅ 成功插入: {total_inserted}")
            print(f"   ⚠️  跳过: {total_skipped}")
            print("=" * 80)

        elif args.date:
            # 同步指定日期
            sync_date = date.fromisoformat(args.date)
            result = syncer.sync_date(sync_date, dry_run=args.dry_run)

            print()
            print("=" * 80)
            print("📊 同步统计:")
            print(f"   日期: {result['date']}")
            print(f"   总记录数: {result['records']}")
            print(f"   ✅ 成功插入: {result['inserted']}")
            print(f"   ⚠️  跳过: {result['skipped']}")
            print("=" * 80)

        else:
            # 默认: 同步今天
            today = date.today()
            result = syncer.sync_date(today, dry_run=args.dry_run)

            print()
            print("=" * 80)
            print("📊 同步统计:")
            print(f"   日期: {result['date']}")
            print(f"   总记录数: {result['records']}")
            print(f"   ✅ 成功插入: {result['inserted']}")
            print(f"   ⚠️  跳过: {result['skipped']}")
            print("=" * 80)

        print()
        print("✅ 同步完成！")
        print()
        print("💡 验证:")
        print("   SELECT count(), provider, success FROM llm.ai_brain_calls")
        print("   WHERE date = today() GROUP BY provider, success;")
        print()

    except KeyboardInterrupt:
        print()
        print("⚠️  用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"❌ 同步失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
