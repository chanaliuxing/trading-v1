#!/bin/bash

# ClickHouse 维护脚本包装器
# 提供便捷的维护命令接口

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLICKHOUSE_CLIENT="docker exec -i trading_clickhouse clickhouse-client"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

function print_usage() {
    cat << EOF
ClickHouse 维护脚本

用法:
    $0 [command]

命令:
    init            初始化 TTL 配置并立即清理（首次运行或重置时使用）
    weekly          执行每周巡检和清理（推荐每周运行一次）
    emergency       紧急清空所有日志（⚠️  仅在磁盘告急时使用）
    check           快速检查磁盘和日志状态
    optimize        手动触发 OPTIMIZE（清理过期数据）
    help            显示此帮助信息

示例:
    $0 init         # 首次配置 TTL
    $0 weekly       # 每周维护
    $0 check        # 快速检查

EOF
}

function run_init() {
    echo -e "${GREEN}执行初始化 TTL 配置...${NC}"
    cat "$SCRIPT_DIR/clickhouse-init-logs.sql" | $CLICKHOUSE_CLIENT
    echo -e "${GREEN}✅ TTL 初始化完成${NC}"
}

function run_weekly() {
    echo -e "${GREEN}执行每周巡检...${NC}"
    cat "$SCRIPT_DIR/clickhouse-weekly-clean.sql" | $CLICKHOUSE_CLIENT
    echo -e "${GREEN}✅ 每周巡检完成${NC}"
}

function run_emergency() {
    echo -e "${RED}⚠️  警告：即将删除所有系统日志数据！${NC}"
    read -p "确定要继续吗？(yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "已取消"
        exit 0
    fi

    echo -e "${YELLOW}执行紧急清理...${NC}"
    cat "$SCRIPT_DIR/clickhouse-emergency-clean.sql" | $CLICKHOUSE_CLIENT
    echo -e "${GREEN}✅ 紧急清理完成${NC}"
}

function run_check() {
    echo -e "${GREEN}检查磁盘和日志状态...${NC}"

    # 磁盘使用情况
    echo -e "\n=== 磁盘使用情况 ==="
    $CLICKHOUSE_CLIENT --query "
        SELECT
            name,
            path,
            formatReadableSize(free_space) AS free,
            formatReadableSize(total_space) AS total,
            round(free_space / total_space * 100, 2) AS free_percent
        FROM system.disks;
    "

    # 日志表体量
    echo -e "\n=== 日志表体量 Top 10 ==="
    $CLICKHOUSE_CLIENT --query "
        SELECT
            table,
            formatReadableSize(sum(bytes)) AS size,
            sum(rows) AS rows
        FROM system.parts
        WHERE database = 'system' AND active = 1
        GROUP BY table
        ORDER BY sum(bytes) DESC
        LIMIT 10;
    "

    # 宿主机目录大小
    echo -e "\n=== 宿主机数据目录大小 ==="
    du -sh /workspace/Documents/clickhouse_data 2>/dev/null || echo "无法访问数据目录"
    du -sh /workspace/Documents/clickhouse_logs 2>/dev/null || echo "无法访问日志目录"
}

function run_optimize() {
    echo -e "${GREEN}手动触发 OPTIMIZE...${NC}"
    $CLICKHOUSE_CLIENT --query "
        OPTIMIZE TABLE system.text_log FINAL;
        OPTIMIZE TABLE system.query_log FINAL;
        OPTIMIZE TABLE system.query_thread_log FINAL;
        OPTIMIZE TABLE system.metric_log FINAL;
        OPTIMIZE TABLE system.asynchronous_metric_log FINAL;
        OPTIMIZE TABLE system.part_log FINAL;
    "
    echo -e "${GREEN}✅ OPTIMIZE 完成${NC}"
}

# 主逻辑
case "${1:-}" in
    init)
        run_init
        ;;
    weekly)
        run_weekly
        ;;
    emergency)
        run_emergency
        ;;
    check)
        run_check
        ;;
    optimize)
        run_optimize
        ;;
    help|--help|-h)
        print_usage
        ;;
    *)
        echo -e "${RED}错误：未知命令 '${1:-}'${NC}\n"
        print_usage
        exit 1
        ;;
esac
