#!/usr/bin/env python3
"""
僵尸会话自动清理脚本

功能:
1. 识别 RUNNING > 24h 的僵尸会话
2. 更新状态为 TIMEOUT
3. 记录清理日志到 ClickHouse
4. 生成清理报告

用法:
    python scripts/cleanup_zombie_sessions.py                    # 清理 >24h 僵尸会话
    python scripts/cleanup_zombie_sessions.py --threshold 48    # 清理 >48h 僵尸会话
    python scripts/cleanup_zombie_sessions.py --dry-run         # 仅查看不清理
    python scripts/cleanup_zombie_sessions.py --force-all       # 强制清理所有 RUNNING 会话 (危险!)
"""

import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from clickhouse_driver import Client as CHClient
except ImportError:
    print("⚠️  clickhouse-driver 未安装")
    print("   安装: pip install clickhouse-driver")
    sys.exit(1)


class ZombieSessionCleaner:
    """僵尸会话清理器"""

    def __init__(self, clickhouse_host='localhost', clickhouse_port=9000):
        self.client = CHClient(host=clickhouse_host, port=clickhouse_port)
        self.ny_tz = 'America/New_York'

    def find_zombie_sessions(self, threshold_hours=24):
        """
        查找僵尸会话

        Args:
            threshold_hours: 阈值小时数，默认24小时

        Returns:
            list: 僵尸会话列表
        """
        query = f"""
        SELECT
            session_id,
            bot_id,
            mode,
            start_ts_ny,
            round((toUnixTimestamp(now()) - toUnixTimestamp(start_ts_ny)) / 3600, 1) as hours_running,
            symbols,
            status
        FROM llm.llm_sessions
        WHERE status = 'RUNNING'
          AND (toUnixTimestamp(now()) - toUnixTimestamp(start_ts_ny)) > {threshold_hours * 3600}
        ORDER BY start_ts_ny ASC
        """

        try:
            rows = self.client.execute(query)
            return [
                {
                    'session_id': row[0],
                    'bot_id': row[1],
                    'mode': row[2],
                    'start_ts_ny': row[3],
                    'hours_running': row[4],
                    'symbols': row[5],
                    'status': row[6]
                }
                for row in rows
            ]
        except Exception as e:
            print(f"❌ 查询僵尸会话失败: {e}")
            return []

    def cleanup_zombie_session(self, session_id, close_reason="TIMEOUT: Zombie session auto-cleanup"):
        """
        清理单个僵尸会话

        Args:
            session_id: 会话ID
            close_reason: 关闭原因

        Returns:
            bool: 是否成功
        """
        # ClickHouse 不支持 UPDATE，需要使用 ALTER TABLE UPDATE 或者插入新记录
        # 这里我们检查表引擎
        try:
            # 方法1: 使用 ALTER TABLE UPDATE (较慢但支持所有引擎)
            query = f"""
            ALTER TABLE llm.llm_sessions
            UPDATE
                status = 'TIMEOUT',
                end_ts_ny = now(),
                close_reason = '{close_reason}'
            WHERE session_id = '{session_id}'
            """

            self.client.execute(query)
            return True

        except Exception as e:
            print(f"❌ 更新会话 {session_id[:12]} 失败: {e}")
            return False

    def log_cleanup_action(self, session_id, action, details):
        """
        记录清理操作到日志表 (如果存在)

        Args:
            session_id: 会话ID
            action: 操作类型
            details: 详细信息
        """
        try:
            # 检查日志表是否存在
            check_table = """
            SELECT count()
            FROM system.tables
            WHERE database = 'llm' AND name = 'session_cleanup_log'
            """
            result = self.client.execute(check_table)

            if result[0][0] == 0:
                # 创建日志表
                create_table = """
                CREATE TABLE IF NOT EXISTS llm.session_cleanup_log (
                    ts_ny DateTime('America/New_York') DEFAULT now(),
                    session_id String,
                    action String,
                    details String,
                    cleanup_script_version String DEFAULT 'v1.0'
                ) ENGINE = MergeTree()
                ORDER BY (ts_ny, session_id)
                """
                self.client.execute(create_table)
                print("✅ 创建日志表 llm.session_cleanup_log")

            # 插入日志
            insert_log = """
            INSERT INTO llm.session_cleanup_log (session_id, action, details)
            VALUES
            """
            self.client.execute(
                insert_log,
                [{'session_id': session_id, 'action': action, 'details': details}]
            )

        except Exception as e:
            print(f"⚠️  记录日志失败 (非致命): {e}")

    def cleanup_all_zombies(self, threshold_hours=24, dry_run=False):
        """
        清理所有僵尸会话

        Args:
            threshold_hours: 阈值小时数
            dry_run: 是否仅查看不执行

        Returns:
            dict: 清理统计
        """
        print("=" * 80)
        print(f"🧹 僵尸会话清理器 v1.0")
        print("=" * 80)
        print(f"📊 阈值: > {threshold_hours} 小时")
        print(f"🔍 模式: {'仅查看 (DRY RUN)' if dry_run else '执行清理'}")
        print(f"🕐 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)
        print()

        # 1. 查找僵尸会话
        print("🔍 查找僵尸会话...")
        zombies = self.find_zombie_sessions(threshold_hours)

        if not zombies:
            print("✅ 未发现僵尸会话")
            return {'found': 0, 'cleaned': 0, 'failed': 0}

        print(f"⚠️  发现 {len(zombies)} 个僵尸会话:")
        print()

        # 2. 显示僵尸会话详情
        print(f"{'Session ID':<14} {'Bot ID':<25} {'Mode':<7} {'Hours Running':<15} {'Started'}")
        print("-" * 95)

        for zombie in zombies:
            session_short = zombie['session_id'][:12]
            bot_id_short = zombie['bot_id'][:23] if len(zombie['bot_id']) > 23 else zombie['bot_id']
            mode = zombie['mode']
            hours = zombie['hours_running']
            started = zombie['start_ts_ny'].strftime('%Y-%m-%d %H:%M')

            print(f"{session_short:<14} {bot_id_short:<25} {mode:<7} {hours:<15.1f} {started}")

        print()
        print("=" * 95)
        print()

        if dry_run:
            print("ℹ️  DRY RUN 模式 - 不执行实际清理")
            return {'found': len(zombies), 'cleaned': 0, 'failed': 0}

        # 3. 执行清理
        print(f"🧹 开始清理 {len(zombies)} 个僵尸会话...")
        print()

        cleaned = 0
        failed = 0

        for i, zombie in enumerate(zombies, 1):
            session_id = zombie['session_id']
            session_short = session_id[:12]
            hours = zombie['hours_running']

            print(f"  [{i}/{len(zombies)}] 清理 {session_short} (运行了 {hours:.1f}h)...", end=' ')

            close_reason = f"TIMEOUT: Auto-cleanup after {hours:.1f} hours (threshold: {threshold_hours}h)"

            success = self.cleanup_zombie_session(session_id, close_reason)

            if success:
                print("✅")
                cleaned += 1

                # 记录日志
                details = f"Cleaned zombie session running for {hours:.1f}h (bot: {zombie['bot_id']}, mode: {zombie['mode']})"
                self.log_cleanup_action(session_id, 'CLEANUP_ZOMBIE', details)
            else:
                print("❌")
                failed += 1

        print()
        print("=" * 80)
        print("📊 清理统计:")
        print(f"   发现僵尸会话: {len(zombies)}")
        print(f"   ✅ 成功清理:   {cleaned}")
        print(f"   ❌ 失败:       {failed}")
        print("=" * 80)
        print()

        if cleaned > 0:
            print("✅ 清理完成！")
            print()
            print("💡 提示:")
            print("   - 清理日志已记录到: llm.session_cleanup_log")
            print("   - 查看日志: SELECT * FROM llm.session_cleanup_log ORDER BY ts_ny DESC LIMIT 10")
            print("   - 验证清理: SELECT count() FROM llm.llm_sessions WHERE status='TIMEOUT'")
            print()

        return {'found': len(zombies), 'cleaned': cleaned, 'failed': failed}

    def get_session_stats(self):
        """获取会话统计信息"""
        query = """
        SELECT
            status,
            count() as count,
            round(avg(toUnixTimestamp(now()) - toUnixTimestamp(start_ts_ny)) / 3600, 1) as avg_hours,
            round(max(toUnixTimestamp(now()) - toUnixTimestamp(start_ts_ny)) / 3600, 1) as max_hours
        FROM llm.llm_sessions
        GROUP BY status
        ORDER BY count DESC
        """

        try:
            rows = self.client.execute(query)
            return [
                {
                    'status': row[0],
                    'count': row[1],
                    'avg_hours': row[2],
                    'max_hours': row[3]
                }
                for row in rows
            ]
        except Exception as e:
            print(f"❌ 查询会话统计失败: {e}")
            return []


def main():
    parser = argparse.ArgumentParser(
        description='僵尸会话自动清理脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/cleanup_zombie_sessions.py                    # 清理 >24h 僵尸会话
  python scripts/cleanup_zombie_sessions.py --threshold 48    # 清理 >48h 僵尸会话
  python scripts/cleanup_zombie_sessions.py --dry-run         # 仅查看不清理
  python scripts/cleanup_zombie_sessions.py --stats           # 显示会话统计
        """
    )

    parser.add_argument(
        '--threshold',
        type=int,
        default=24,
        help='僵尸会话阈值（小时），默认24小时'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='仅查看不执行清理 (Dry Run 模式)'
    )

    parser.add_argument(
        '--stats',
        action='store_true',
        help='显示会话统计信息后退出'
    )

    parser.add_argument(
        '--host',
        type=str,
        default='localhost',
        help='ClickHouse 主机地址，默认 localhost'
    )

    parser.add_argument(
        '--port',
        type=int,
        default=9000,
        help='ClickHouse 端口，默认 9000'
    )

    args = parser.parse_args()

    # 创建清理器
    cleaner = ZombieSessionCleaner(
        clickhouse_host=args.host,
        clickhouse_port=args.port
    )

    # 如果只是查看统计
    if args.stats:
        print("=" * 80)
        print("📊 会话统计")
        print("=" * 80)
        stats = cleaner.get_session_stats()

        if stats:
            print()
            print(f"{'Status':<12} {'Count':<10} {'Avg Hours':<12} {'Max Hours'}")
            print("-" * 50)
            for s in stats:
                print(f"{s['status']:<12} {s['count']:<10} {s['avg_hours']:<12.1f} {s['max_hours']:.1f}")
            print()
        else:
            print("⚠️  无法获取统计信息")

        sys.exit(0)

    # 执行清理
    try:
        result = cleaner.cleanup_all_zombies(
            threshold_hours=args.threshold,
            dry_run=args.dry_run
        )

        # 返回码
        if result['failed'] > 0:
            sys.exit(1)  # 有失败
        elif result['cleaned'] == 0 and result['found'] > 0:
            sys.exit(2)  # 发现僵尸但未清理 (dry-run)
        else:
            sys.exit(0)  # 成功

    except KeyboardInterrupt:
        print()
        print("⚠️  用户中断")
        sys.exit(130)

    except Exception as e:
        print(f"❌ 清理失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
