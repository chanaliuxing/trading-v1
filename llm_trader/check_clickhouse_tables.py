#!/usr/bin/env python3
"""检查ClickHouse中有哪些表和数据"""

import clickhouse_connect
from dotenv import load_dotenv
import os

load_dotenv()

ch_host = os.getenv("CLICKHOUSE_HOST", "localhost")
ch_port = int(os.getenv("CLICKHOUSE_PORT", "8123"))
ch_user = os.getenv("CLICKHOUSE_USER", "default")
ch_password = os.getenv("CLICKHOUSE_PASSWORD", "")

print("=" * 80)
print("ClickHouse 数据库和表检查")
print("=" * 80)

try:
    client = clickhouse_connect.get_client(
        host=ch_host,
        port=ch_port,
        username=ch_user,
        password=ch_password,
    )

    print(f"\n✅ 连接成功: {ch_host}:{ch_port}")

    # 查看所有数据库
    print("\n" + "=" * 80)
    print("所有数据库:")
    print("=" * 80)
    result = client.query("SHOW DATABASES")
    for row in result.result_rows:
        print(f"  - {row[0]}")

    # 查看 sfs 数据库的表
    print("\n" + "=" * 80)
    print("SFS 数据库的表:")
    print("=" * 80)
    result = client.query("SHOW TABLES FROM sfs")
    sfs_tables = [row[0] for row in result.result_rows]
    for table in sfs_tables:
        print(f"  - {table}")

    # 检查每个表的数据量
    print("\n" + "=" * 80)
    print("各表数据统计:")
    print("=" * 80)

    for table in sfs_tables:
        try:
            count_result = client.query(f"SELECT count() FROM sfs.{table}")
            count = count_result.result_rows[0][0]

            # 获取最新记录时间
            latest_result = client.query(f"SELECT max(ts) FROM sfs.{table}")
            latest_ts = latest_result.result_rows[0][0] if latest_result.result_rows else None

            print(f"\n  {table}:")
            print(f"    总记录数: {count:,}")
            print(f"    最新时间: {latest_ts}")

            # 如果有数据，查看最新一条
            if count > 0:
                sample_result = client.query(f"SELECT * FROM sfs.{table} ORDER BY ts DESC LIMIT 1")
                print(f"    列数: {len(sample_result.result_rows[0]) if sample_result.result_rows else 0}")

        except Exception as e:
            print(f"  {table}: ❌ 查询失败: {e}")

    # 检查 trading 数据库
    print("\n" + "=" * 80)
    print("Trading 数据库的表:")
    print("=" * 80)
    result = client.query("SHOW TABLES FROM trading")
    trading_tables = [row[0] for row in result.result_rows]
    for table in trading_tables:
        print(f"  - {table}")

        try:
            count_result = client.query(f"SELECT count() FROM trading.{table}")
            count = count_result.result_rows[0][0]
            print(f"    总记录数: {count:,}")
        except Exception as e:
            print(f"    ❌ 查询失败: {e}")

    # 重点：检查是否有1s数据的表
    print("\n" + "=" * 80)
    print("🔍 寻找 1s bars 数据:")
    print("=" * 80)

    # 检查 sfs.sfs_5s 表（5s bars）
    if "sfs_5s" in sfs_tables:
        print("\n✅ 找到 sfs.sfs_5s (5s bars)")
        result = client.query("""
            SELECT symbol, count() as cnt, max(ts) as latest
            FROM sfs.sfs_5s
            WHERE symbol IN ('AAPL', 'TSLA', 'NVDA')
            GROUP BY symbol
            ORDER BY symbol
        """)
        print("\n  各symbol统计:")
        for row in result.result_rows:
            print(f"    {row[0]}: {row[1]:,} 条, 最新: {row[2]}")

    # 检查是否有1s数据的表
    one_s_tables = [t for t in sfs_tables if '1s' in t.lower() or 'tick' in t.lower()]
    if one_s_tables:
        print(f"\n✅ 找到可能的1s数据表: {one_s_tables}")
    else:
        print("\n⚠️  未找到1s数据表")
        print("   只有Redis中有1s bars (key: sfs:1s:SYMBOL)")

    client.close()

except Exception as e:
    print(f"\n❌ 连接失败: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 80)
print("检查完成")
print("=" * 80)
