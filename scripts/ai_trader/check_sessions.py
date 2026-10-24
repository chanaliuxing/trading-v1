#!/usr/bin/env python3
"""
查看今日 LLM Trader 会话记录（从 ClickHouse）
"""
import sys
from datetime import date
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

try:
    from clickhouse_driver import Client as CHClient
except ImportError:
    print("⚠️  clickhouse-driver 未安装，无法查询数据库")
    print("   安装: pip install clickhouse-driver")
    sys.exit(1)

def check_sessions():
    """查询今日会话（从 ClickHouse）"""
    try:
        client = CHClient(host='localhost')

        # 检查表是否存在
        result = client.execute("""
            SELECT count()
            FROM system.tables
            WHERE database = 'llm' AND name = 'llm_sessions'
        """)

        if result[0][0] == 0:
            print("⚠️  llm.llm_sessions 表不存在")
            return

        # 查询今日会话
        today = date.today().isoformat()
        rows = client.execute(f"""
            SELECT
                substring(session_id, 1, 8) as session_short,
                bot_id,
                mode,
                formatDateTime(start_ts_ny, '%Y-%m-%d %H:%i:%S') as started,
                status,
                initial_cash,
                symbols
            FROM llm.llm_sessions
            WHERE trading_day = toDate('{today}')
            ORDER BY start_ts_ny DESC
            LIMIT 10
        """)

        if rows:
            print("┌─ 📊 ClickHouse 会话（今日）")
            print("└────────────────────────────────────────────────────────────────────────────")
            print(f"{'Session':<10} {'Bot ID':<30} {'Mode':<8} {'Started':<20} {'Symbols'}")
            print("-" * 100)

            for row in rows:
                session_short = row[0] or 'N/A'
                bot_id = (row[1][:28] if len(row[1]) <= 28 else row[1][:25] + '...') if row[1] else 'N/A'
                mode = row[2] or 'N/A'
                started = row[3] or 'N/A'
                symbols_str = row[6] or 'N/A'

                print(f"{session_short:<10} {bot_id:<30} {mode:<8} {started:<20} {symbols_str}")

            # 统计信息
            count_result = client.execute(f"""
                SELECT
                    count() as total,
                    countIf(status = 'OPEN') as open_sessions,
                    countIf(status = 'CLOSED') as closed_sessions
                FROM llm.llm_sessions
                WHERE trading_day = toDate('{today}')
            """)

            if count_result:
                total, open_sessions, closed = count_result[0]
                print("-" * 100)
                print(f"统计: 总计 {total} 个会话 | 运行中 {open_sessions} | 已完成 {closed}")
        else:
            print("⚠️  今天无会话记录")

    except Exception as e:
        print(f"⚠️  ClickHouse 查询失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_sessions()
