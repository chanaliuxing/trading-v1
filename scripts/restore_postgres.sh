#!/bin/bash
# PostgreSQL 独立恢复脚本
# 用途：在新机器上恢复 PostgreSQL 备份

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${GREEN}=== PostgreSQL 数据库恢复工具 ===${NC}"

# 查找备份目录
echo -e "\n${YELLOW}查找 PostgreSQL 备份...${NC}"
BACKUP_DIRS=$(find ~ -maxdepth 1 -type d -name "postgres_backup_*" 2>/dev/null | sort -r)

if [ -z "$BACKUP_DIRS" ]; then
    echo -e "${RED}错误: 未找到备份目录${NC}"
    echo "请确保备份目录在 ~/ 下，格式为 postgres_backup_YYYYMMDD_HHMMSS"
    echo ""
    echo "或手动指定备份目录:"
    echo "  cd /path/to/backup"
    echo "  bash restore_postgres.sh"
    exit 1
fi

# 选择备份（如果有多个）
if [ $(echo "$BACKUP_DIRS" | wc -l) -gt 1 ]; then
    echo "找到多个备份:"
    echo "$BACKUP_DIRS" | nl
    echo ""
    read -p "选择备份编号 [1]: " CHOICE
    CHOICE=${CHOICE:-1}
    BACKUP_DIR=$(echo "$BACKUP_DIRS" | sed -n "${CHOICE}p")
else
    BACKUP_DIR=$(echo "$BACKUP_DIRS" | head -1)
fi

if [ ! -d "$BACKUP_DIR" ]; then
    # 可能在当前目录
    if [ -f "backup.dump" ] || [ -f "full_backup.sql" ]; then
        BACKUP_DIR="."
        echo "使用当前目录的备份文件"
    else
        echo -e "${RED}错误: 备份目录不存在${NC}"
        exit 1
    fi
fi

echo -e "使用备份: ${GREEN}$BACKUP_DIR${NC}"
cd "$BACKUP_DIR"

# 配置（从环境变量或 .env 文件读取）
if [ -f "../../.env" ]; then
    echo "加载配置: ../../.env"
    source ../../.env
fi

DB_USER="${PG_USER:-postgres}"
DB_NAME="${PG_DB:-test}"
DB_HOST="${PG_HOST:-localhost}"
DB_PORT="${PG_PORT:-5432}"

echo ""
echo "目标数据库配置:"
echo "  主机: $DB_HOST:$DB_PORT"
echo "  数据库: $DB_NAME"
echo "  用户: $DB_USER"
echo ""
read -p "配置正确吗？(y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "请设置环境变量后重试:"
    echo "  export PG_USER=your_user"
    echo "  export PG_DB=your_db"
    echo "  export PG_HOST=localhost"
    echo "  export PG_PORT=5432"
    exit 0
fi

# 检查 PostgreSQL
echo -e "\n${YELLOW}[1/5] 检查 PostgreSQL...${NC}"
if ! command -v psql &> /dev/null; then
    echo -e "${RED}错误: 未安装 PostgreSQL${NC}"
    echo ""
    echo "请安装 PostgreSQL:"
    echo "  macOS: brew install postgresql@16"
    echo "  Ubuntu: sudo apt-get install postgresql-client"
    echo "  CentOS: sudo yum install postgresql"
    exit 1
fi

if ! pg_isready -h $DB_HOST -p $DB_PORT > /dev/null 2>&1; then
    echo -e "${RED}错误: PostgreSQL 未运行${NC}"
    echo ""
    echo "请启动 PostgreSQL:"
    echo "  macOS: brew services start postgresql@16"
    echo "  Linux: sudo systemctl start postgresql"
    exit 1
fi

echo -e "${GREEN}✓ PostgreSQL 运行中${NC}"

# 检查版本
PG_VERSION=$(psql -V | awk '{print $3}')
echo "PostgreSQL 版本: $PG_VERSION"

# 检查数据库
echo -e "\n${YELLOW}[2/5] 检查目标数据库...${NC}"
DB_EXISTS=$(psql -U $DB_USER -h $DB_HOST -p $DB_PORT -lqt | cut -d \| -f 1 | grep -w $DB_NAME | wc -l)

if [ "$DB_EXISTS" -eq 0 ]; then
    echo -e "${YELLOW}数据库 '$DB_NAME' 不存在${NC}"
    read -p "是否创建？(y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        createdb -U $DB_USER -h $DB_HOST -p $DB_PORT $DB_NAME
        echo -e "${GREEN}✓ 数据库创建成功${NC}"
    else
        echo "已取消"
        exit 0
    fi
else
    # 检查是否有表
    TABLE_COUNT=$(psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -t -c "
      SELECT COUNT(*)
      FROM pg_tables
      WHERE schemaname NOT IN ('pg_catalog', 'information_schema');
    " 2>/dev/null | xargs || echo "0")

    if [ "$TABLE_COUNT" -gt 0 ]; then
        echo -e "${YELLOW}警告: 数据库 '$DB_NAME' 已存在且有 $TABLE_COUNT 张表${NC}"
        echo "恢复将覆盖现有数据"
        read -p "继续？(y/n) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "已取消"
            exit 0
        fi
    else
        echo -e "${GREEN}✓ 数据库存在且为空${NC}"
    fi
fi

# 选择恢复文件和方法
echo -e "\n${YELLOW}[3/5] 选择恢复方式...${NC}"

RESTORE_FILE=""
RESTORE_CMD=""

if [ -f "backup.dump" ]; then
    RESTORE_FILE="backup.dump"
    RESTORE_CMD="pg_restore"
    FILE_SIZE=$(du -h backup.dump | cut -f1)
    echo -e "${GREEN}✓ 找到压缩备份: backup.dump ($FILE_SIZE)${NC}"
    echo "  使用 pg_restore（推荐，最快）"
elif [ -f "full_backup.sql" ]; then
    RESTORE_FILE="full_backup.sql"
    RESTORE_CMD="psql"
    FILE_SIZE=$(du -h full_backup.sql | cut -f1)
    echo -e "${GREEN}✓ 找到 SQL 备份: full_backup.sql ($FILE_SIZE)${NC}"
    echo "  使用 psql"
else
    echo -e "${RED}错误: 未找到备份文件${NC}"
    echo "需要以下文件之一:"
    echo "  - backup.dump (压缩格式，推荐)"
    echo "  - full_backup.sql (SQL 格式)"
    ls -la
    exit 1
fi

# 预览备份内容
echo -e "\n${YELLOW}[4/5] 预览备份内容...${NC}"
if [ "$RESTORE_CMD" = "pg_restore" ]; then
    echo "备份包含的表:"
    pg_restore --list backup.dump | grep "TABLE DATA" | head -10
    echo "..."
else
    echo "SQL 文件大小: $(wc -l < full_backup.sql) 行"
fi

echo ""
read -p "开始恢复？(y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "已取消"
    exit 0
fi

# 恢复数据
echo -e "\n${YELLOW}[5/5] 恢复数据库...${NC}"
echo "这可能需要几秒到几分钟..."
echo ""

if [ "$RESTORE_CMD" = "pg_restore" ]; then
    # 使用 pg_restore（压缩格式）
    pg_restore -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT \
        --clean \
        --if-exists \
        --no-owner \
        --no-privileges \
        --verbose \
        $RESTORE_FILE 2>&1 | tee restore.log
else
    # 使用 psql（SQL 格式）
    psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT \
        -f $RESTORE_FILE 2>&1 | tee restore.log
fi

RESTORE_EXIT_CODE=$?

if [ $RESTORE_EXIT_CODE -ne 0 ]; then
    echo -e "\n${YELLOW}注意: 恢复过程中有一些警告或错误${NC}"
    echo "这通常是正常的（例如清理不存在的表）"
    echo "日志已保存到: restore.log"
    echo ""
    read -p "继续验证？(y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# 验证恢复
echo -e "\n${BLUE}===========================================${NC}"
echo -e "${BLUE}验证恢复结果${NC}"
echo -e "${BLUE}===========================================${NC}"

echo -e "\n${YELLOW}Schema 统计:${NC}"
psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -c "
  SELECT
    schemaname,
    COUNT(*) as table_count,
    pg_size_pretty(SUM(pg_total_relation_size(schemaname||'.'||tablename))) as total_size
  FROM pg_tables
  WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  GROUP BY schemaname
  ORDER BY schemaname;
"

echo -e "\n${YELLOW}表列表（前20张）:${NC}"
psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -c "
  SELECT
    schemaname || '.' || tablename as table_name,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
  FROM pg_tables
  WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
  LIMIT 20;
"

# 对比备份统计（如果有）
if [ -f "tables_stats.txt" ]; then
    echo -e "\n${YELLOW}原始备份统计:${NC}"
    head -5 tables_stats.txt
    echo "..."

    ORIG_COUNT=$(grep -c "core" tables_stats.txt || echo "0")
    CURR_COUNT=$(psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -t -c "
      SELECT COUNT(*)
      FROM pg_tables
      WHERE schemaname = 'core';
    " | xargs)

    echo ""
    echo "表数量对比:"
    echo "  原始: $ORIG_COUNT 张表"
    echo "  当前: $CURR_COUNT 张表"

    if [ "$ORIG_COUNT" = "$CURR_COUNT" ]; then
        echo -e "  ${GREEN}✓ 表数量一致${NC}"
    else
        echo -e "  ${YELLOW}⚠ 表数量不一致（可能正常，某些表可能为空）${NC}"
    fi
fi

# 完成
echo -e "\n${GREEN}===========================================${NC}"
echo -e "${GREEN}PostgreSQL 恢复完成！${NC}"
echo -e "${GREEN}===========================================${NC}"

echo -e "\n${YELLOW}连接信息:${NC}"
echo "  主机: $DB_HOST:$DB_PORT"
echo "  数据库: $DB_NAME"
echo "  用户: $DB_USER"

echo -e "\n${YELLOW}测试连接:${NC}"
echo "  psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT"

echo -e "\n${YELLOW}查看数据:${NC}"
echo "  # 查看订单表"
echo "  psql -U $DB_USER -d $DB_NAME -c 'SELECT * FROM core.core_order LIMIT 5;'"
echo ""
echo "  # 查看策略配置"
echo "  psql -U $DB_USER -d $DB_NAME -c 'SELECT * FROM core.core_strategy;'"

echo -e "\n${YELLOW}日志文件:${NC}"
echo "  restore.log - 详细恢复日志"

echo -e "\n${GREEN}恢复流程全部完成！${NC}"
