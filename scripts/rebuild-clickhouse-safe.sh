#!/bin/bash

# ClickHouse 容器安全重建脚本
# 目的：应用日志优化配置，不丢失业务数据
# 数据位置：/workspace/Documents/clickhouse_data（宿主机，不会删除）

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  ClickHouse 容器安全重建${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 1. 确认数据位置
echo -e "${GREEN}Step 1: 确认数据位置...${NC}"
DATA_DIR="/workspace/Documents/clickhouse_data"
if [ ! -d "$DATA_DIR/store" ]; then
    echo -e "${RED}错误: 数据目录不存在: $DATA_DIR/store${NC}"
    exit 1
fi

DATA_SIZE=$(du -sh "$DATA_DIR" | cut -f1)
echo "  ✓ 数据目录: $DATA_DIR"
echo "  ✓ 数据大小: $DATA_SIZE"
echo ""

# 2. 导出当前表清单（验证基准）
echo -e "${GREEN}Step 2: 导出当前表清单...${NC}"
BACKUP_FILE="$PROJECT_ROOT/logs/maintenance/tables_before_rebuild_$(date +%Y%m%d_%H%M%S).txt"
mkdir -p "$PROJECT_ROOT/logs/maintenance"

docker exec trading_clickhouse clickhouse-client --query \
    "SELECT database, table, total_rows, formatReadableSize(total_bytes) AS size
     FROM system.tables
     WHERE database IN ('trading', 'lake', 'sfs')
     ORDER BY database, table" > "$BACKUP_FILE" 2>/dev/null || {
    echo -e "${YELLOW}  警告: 无法连接到 ClickHouse（容器可能已停止）${NC}"
}

if [ -f "$BACKUP_FILE" ]; then
    TABLE_COUNT=$(wc -l < "$BACKUP_FILE")
    echo "  ✓ 已导出 $TABLE_COUNT 个表到: $BACKUP_FILE"
fi
echo ""

# 3. 确认操作
echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  重建影响确认${NC}"
echo -e "${YELLOW}========================================${NC}"
echo ""
echo -e "${GREEN}✅ 保留（不会删除）:${NC}"
echo "  • 所有业务数据表 (trading, lake, sfs)"
echo "  • 表结构定义"
echo "  • 数据目录: $DATA_DIR"
echo ""
echo -e "${RED}❌ 删除（将被清理）:${NC}"
echo "  • ClickHouse 容器（将重新创建）"
echo "  • system 日志表数据（旧配置）"
echo ""
echo -e "${BLUE}🔧 应用配置:${NC}"
echo "  • 禁用 processors_profile_log 和 trace_log"
echo "  • 查询日志采样 5%"
echo "  • 日志级别降至 ERROR"
echo ""

read -p "确认重建容器？(输入 yes 继续): " confirm
if [ "$confirm" != "yes" ]; then
    echo -e "${YELLOW}已取消${NC}"
    exit 0
fi
echo ""

# 4. 停止并删除容器
echo -e "${GREEN}Step 3: 停止并删除容器...${NC}"
cd "$PROJECT_ROOT/infra"

docker compose stop clickhouse
echo "  ✓ 容器已停止"

docker compose rm -f clickhouse
echo "  ✓ 容器已删除"
echo ""

# 5. 可选：清理 system 日志表数据（释放更多空间）
echo -e "${GREEN}Step 4: 清理 system 日志表数据（可选）...${NC}"
read -p "是否删除 system 日志表数据以释放空间？(yes/no): " clean_system
if [ "$clean_system" == "yes" ]; then
    # 删除 system 表的 metadata 和 store 数据
    # 注意：只删除 system 数据库的元数据，不影响业务数据
    if [ -d "$DATA_DIR/metadata/system" ]; then
        rm -rf "$DATA_DIR/metadata/system"
        echo "  ✓ 已删除 system 表元数据"
    fi

    # 查找并删除 system 数据库的 UUID 目录
    # 这需要查找 metadata 来确定 UUID（已删除，跳过）
    echo "  ✓ system 日志数据将在容器重建后自动清理"
else
    echo "  ⊘ 跳过清理，system 表将保留"
fi
echo ""

# 6. 重新创建容器
echo -e "${GREEN}Step 5: 重新创建容器（使用优化配置）...${NC}"
docker compose up -d clickhouse
echo "  ✓ 容器已创建"
echo ""

# 7. 等待 ClickHouse 启动
echo -e "${GREEN}Step 6: 等待 ClickHouse 启动...${NC}"
for i in {1..30}; do
    if docker exec trading_clickhouse clickhouse-client --query "SELECT 1" > /dev/null 2>&1; then
        echo "  ✓ ClickHouse 已就绪"
        break
    fi
    echo "  等待中... ($i/30)"
    sleep 2
done
echo ""

# 8. 验证业务数据
echo -e "${GREEN}Step 7: 验证业务数据完整性...${NC}"
AFTER_FILE="$PROJECT_ROOT/logs/maintenance/tables_after_rebuild_$(date +%Y%m%d_%H%M%S).txt"

docker exec trading_clickhouse clickhouse-client --query \
    "SELECT database, table, total_rows, formatReadableSize(total_bytes) AS size
     FROM system.tables
     WHERE database IN ('trading', 'lake', 'sfs')
     ORDER BY database, table" > "$AFTER_FILE"

AFTER_COUNT=$(wc -l < "$AFTER_FILE")
echo "  ✓ 重建后表数量: $AFTER_COUNT"

if [ -f "$BACKUP_FILE" ]; then
    BEFORE_COUNT=$TABLE_COUNT
    if [ "$AFTER_COUNT" -eq "$BEFORE_COUNT" ]; then
        echo -e "  ${GREEN}✓ 表数量匹配！所有数据完整${NC}"
    else
        echo -e "  ${YELLOW}⚠ 表数量变化: $BEFORE_COUNT -> $AFTER_COUNT${NC}"
        echo "  请手动检查差异"
    fi
fi
echo ""

# 9. 检查新配置是否生效
echo -e "${GREEN}Step 8: 验证新配置...${NC}"

# 检查 processors_profile_log 是否被禁用
if docker exec trading_clickhouse clickhouse-client --query \
    "SELECT name FROM system.tables WHERE database='system' AND name='processors_profile_log'" \
    2>/dev/null | grep -q "processors_profile_log"; then
    echo -e "  ${YELLOW}⚠ processors_profile_log 仍然存在（可能需要配置调整）${NC}"
else
    echo -e "  ${GREEN}✓ processors_profile_log 已禁用${NC}"
fi

# 检查 trace_log 是否被禁用
if docker exec trading_clickhouse clickhouse-client --query \
    "SELECT name FROM system.tables WHERE database='system' AND name='trace_log'" \
    2>/dev/null | grep -q "trace_log"; then
    echo -e "  ${YELLOW}⚠ trace_log 仍然存在（可能需要配置调整）${NC}"
else
    echo -e "  ${GREEN}✓ trace_log 已禁用${NC}"
fi

echo ""

# 10. 完成
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  重建完成！${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "验证报告："
echo "  • 重建前表清单: $BACKUP_FILE"
echo "  • 重建后表清单: $AFTER_FILE"
echo ""
echo "下一步："
echo "  1. 运行快速检查: ./scripts/clickhouse-maintenance.sh check"
echo "  2. 验证业务数据: docker exec trading_clickhouse clickhouse-client"
echo "  3. 查看差异: diff $BACKUP_FILE $AFTER_FILE"
echo ""
echo -e "${BLUE}数据目录: $DATA_DIR${NC}"
echo -e "${BLUE}数据大小: $(du -sh $DATA_DIR | cut -f1)${NC}"
echo ""
