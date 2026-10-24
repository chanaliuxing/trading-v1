#!/bin/bash

# ClickHouse 定期巡检 Crontab 设置脚本
# 用途：安装、查看、删除 ClickHouse 定期维护任务

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRONTAB_FILE="$SCRIPT_DIR/clickhouse-crontab.txt"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

function print_usage() {
    cat << EOF
${BLUE}ClickHouse 定期巡检 Crontab 设置${NC}

用法:
    $0 [command]

命令:
    install         安装 crontab 定期任务
    uninstall       删除 crontab 定期任务
    show            显示当前 crontab 配置
    test            测试巡检脚本（立即执行一次）
    logs            查看最近的巡检日志
    help            显示此帮助信息

示例:
    $0 install      # 安装定期任务
    $0 show         # 查看当前配置
    $0 test         # 测试执行
    $0 logs         # 查看日志

EOF
}

function install_crontab() {
    echo -e "${GREEN}准备安装 ClickHouse 定期巡检任务...${NC}"

    # 检查 crontab 文件是否存在
    if [ ! -f "$CRONTAB_FILE" ]; then
        echo -e "${RED}错误: 找不到 crontab 配置文件: $CRONTAB_FILE${NC}"
        exit 1
    fi

    # 检查维护脚本是否存在且可执行
    if [ ! -x "$SCRIPT_DIR/clickhouse-maintenance.sh" ]; then
        echo -e "${RED}错误: 维护脚本不存在或不可执行: $SCRIPT_DIR/clickhouse-maintenance.sh${NC}"
        exit 1
    fi

    # 创建日志目录
    mkdir -p "$PROJECT_ROOT/logs/maintenance"

    # 获取当前 crontab
    TEMP_CRON=$(mktemp)
    crontab -l > "$TEMP_CRON" 2>/dev/null || true

    # 检查是否已经安装
    if grep -q "clickhouse-maintenance.sh weekly" "$TEMP_CRON" 2>/dev/null; then
        echo -e "${YELLOW}⚠️  检测到已安装的 ClickHouse 巡检任务${NC}"
        read -p "是否要重新安装？(yes/no): " confirm
        if [ "$confirm" != "yes" ]; then
            echo "已取消"
            rm "$TEMP_CRON"
            exit 0
        fi

        # 删除旧的任务
        grep -v "clickhouse-maintenance.sh" "$TEMP_CRON" > "${TEMP_CRON}.new" || true
        mv "${TEMP_CRON}.new" "$TEMP_CRON"
    fi

    # 添加新任务
    echo "" >> "$TEMP_CRON"
    cat "$CRONTAB_FILE" >> "$TEMP_CRON"

    # 安装新的 crontab
    crontab "$TEMP_CRON"
    rm "$TEMP_CRON"

    echo -e "${GREEN}✅ ClickHouse 定期巡检任务已成功安装${NC}"
    echo ""
    echo "定期任务配置："
    echo "  • 每周日凌晨 2:00 执行完整巡检和清理"
    echo "  • 日志位置: $PROJECT_ROOT/logs/maintenance/"
    echo ""
    echo "查看已安装的任务："
    echo "  $0 show"
    echo ""
    echo "测试执行："
    echo "  $0 test"
}

function uninstall_crontab() {
    echo -e "${YELLOW}准备删除 ClickHouse 定期巡检任务...${NC}"

    # 获取当前 crontab
    TEMP_CRON=$(mktemp)
    crontab -l > "$TEMP_CRON" 2>/dev/null || true

    # 检查是否已安装
    if ! grep -q "clickhouse-maintenance.sh" "$TEMP_CRON" 2>/dev/null; then
        echo -e "${YELLOW}未检测到 ClickHouse 巡检任务${NC}"
        rm "$TEMP_CRON"
        exit 0
    fi

    # 确认删除
    read -p "确定要删除 ClickHouse 巡检任务吗？(yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "已取消"
        rm "$TEMP_CRON"
        exit 0
    fi

    # 删除任务
    grep -v "clickhouse-maintenance.sh" "$TEMP_CRON" > "${TEMP_CRON}.new" || true
    # 删除 ClickHouse 注释行
    grep -v "# ClickHouse 定期维护任务" "${TEMP_CRON}.new" > "${TEMP_CRON}.final" 2>/dev/null || true

    # 安装清理后的 crontab
    crontab "${TEMP_CRON}.final"
    rm "$TEMP_CRON" "${TEMP_CRON}.new" "${TEMP_CRON}.final"

    echo -e "${GREEN}✅ ClickHouse 定期巡检任务已删除${NC}"
}

function show_crontab() {
    echo -e "${BLUE}=== 当前 Crontab 配置 ===${NC}"

    if ! crontab -l 2>/dev/null | grep -q "clickhouse-maintenance.sh"; then
        echo -e "${YELLOW}未安装 ClickHouse 巡检任务${NC}"
        echo ""
        echo "安装任务："
        echo "  $0 install"
        return
    fi

    echo ""
    crontab -l | grep -A 5 -B 5 "clickhouse-maintenance.sh" || true
    echo ""
    echo -e "${GREEN}✓ ClickHouse 巡检任务已安装${NC}"
}

function test_run() {
    echo -e "${GREEN}执行 ClickHouse 巡检测试...${NC}"
    echo ""

    # 检查脚本是否存在
    if [ ! -x "$SCRIPT_DIR/clickhouse-maintenance.sh" ]; then
        echo -e "${RED}错误: 维护脚本不存在或不可执行${NC}"
        exit 1
    fi

    # 创建日志目录
    mkdir -p "$PROJECT_ROOT/logs/maintenance"

    # 执行测试
    LOG_FILE="$PROJECT_ROOT/logs/maintenance/clickhouse-test-$(date +%Y-%m-%d-%H%M%S).log"

    echo "执行命令: cd $PROJECT_ROOT && ./scripts/clickhouse-maintenance.sh weekly"
    echo "日志文件: $LOG_FILE"
    echo ""

    cd "$PROJECT_ROOT" && ./scripts/clickhouse-maintenance.sh weekly | tee "$LOG_FILE"

    echo ""
    echo -e "${GREEN}✅ 测试完成${NC}"
    echo "日志已保存到: $LOG_FILE"
}

function show_logs() {
    echo -e "${BLUE}=== 最近的巡检日志 ===${NC}"
    echo ""

    LOG_DIR="$PROJECT_ROOT/logs/maintenance"

    if [ ! -d "$LOG_DIR" ]; then
        echo -e "${YELLOW}日志目录不存在: $LOG_DIR${NC}"
        return
    fi

    # 列出最近的日志文件
    LATEST_LOGS=$(ls -t "$LOG_DIR"/clickhouse-*.log 2>/dev/null | head -5)

    if [ -z "$LATEST_LOGS" ]; then
        echo -e "${YELLOW}未找到巡检日志${NC}"
        echo ""
        echo "执行测试以生成日志："
        echo "  $0 test"
        return
    fi

    echo "最近的 5 个日志文件："
    echo ""

    for log in $LATEST_LOGS; do
        size=$(du -h "$log" | cut -f1)
        echo "  $(basename "$log") - $size"
    done

    echo ""
    echo "查看最新日志："
    LATEST_LOG=$(echo "$LATEST_LOGS" | head -1)
    echo "  cat $LATEST_LOG"
    echo ""

    read -p "是否查看最新日志？(yes/no): " confirm
    if [ "$confirm" == "yes" ]; then
        echo ""
        echo -e "${BLUE}=== $(basename "$LATEST_LOG") ===${NC}"
        echo ""
        cat "$LATEST_LOG"
    fi
}

# 主逻辑
case "${1:-}" in
    install)
        install_crontab
        ;;
    uninstall)
        uninstall_crontab
        ;;
    show)
        show_crontab
        ;;
    test)
        test_run
        ;;
    logs)
        show_logs
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
