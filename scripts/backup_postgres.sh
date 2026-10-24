#!/bin/bash
# PostgreSQL 专用备份脚本
# 用途：备份 PostgreSQL 数据库（用于本地安装的 PostgreSQL）

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== PostgreSQL 数据库备份工具 ===${NC}"

# 配置
DB_USER="${PG_USER:-postgres}"
DB_NAME="${PG_DB:-test}"
DB_HOST="${PG_HOST:-localhost}"
DB_PORT="${PG_PORT:-5432}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=~/postgres_backup_${TIMESTAMP}
mkdir -p $BACKUP_DIR

echo "数据库配置:"
echo "  主机: $DB_HOST:$DB_PORT"
echo "  数据库: $DB_NAME"
echo "  用户: $DB_USER"
echo "  备份目录: $BACKUP_DIR"
echo ""

# 检查连接
echo -e "${YELLOW}[1/5] 检查 PostgreSQL 连接...${NC}"
if ! psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -c "SELECT version();" > /dev/null 2>&1; then
    echo -e "${RED}错误: 无法连接到 PostgreSQL${NC}"
    echo "请检查："
    echo "  1. PostgreSQL 是否运行: pg_isready -h $DB_HOST -p $DB_PORT"
    echo "  2. 用户名和密码是否正确"
    echo "  3. 数据库是否存在: psql -U $DB_USER -l"
    exit 1
fi
echo -e "${GREEN}✓ 连接成功${NC}"

# 获取数据库信息
echo -e "\n${YELLOW}[2/5] 获取数据库统计信息...${NC}"

# 表统计
psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -c "
  SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
  FROM pg_tables
  WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
" > $BACKUP_DIR/tables_stats.txt

echo "表统计信息:"
cat $BACKUP_DIR/tables_stats.txt

# 数据库大小
DB_SIZE=$(psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -t -c "
  SELECT pg_size_pretty(pg_database_size('$DB_NAME'));
" | xargs)
echo ""
echo "数据库总大小: $DB_SIZE"

# 3. 导出数据库结构
echo -e "\n${YELLOW}[3/5] 导出数据库结构...${NC}"
pg_dump -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT \
  --schema-only \
  --no-owner \
  --no-privileges \
  -f $BACKUP_DIR/schema.sql

echo -e "${GREEN}✓ 结构导出完成: schema.sql${NC}"

# 4. 导出完整数据（包含结构和数据）
echo -e "\n${YELLOW}[4/5] 导出完整数据库...${NC}"
pg_dump -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT \
  --no-owner \
  --no-privileges \
  -f $BACKUP_DIR/full_backup.sql

echo -e "${GREEN}✓ 完整备份完成: full_backup.sql${NC}"

# 5. 导出自定义格式（压缩，更快恢复）
echo -e "\n${YELLOW}[5/5] 导出压缩格式备份...${NC}"
pg_dump -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT \
  --format=custom \
  --no-owner \
  --no-privileges \
  -f $BACKUP_DIR/backup.dump

echo -e "${GREEN}✓ 压缩备份完成: backup.dump${NC}"

# 生成恢复说明
cat > $BACKUP_DIR/README.txt <<EOF
==========================================
PostgreSQL 数据库备份
==========================================

备份时间: $(date)
数据库: $DB_NAME
用户: $DB_USER
大小: $DB_SIZE

备份文件说明:
--------------
1. schema.sql       - 仅数据库结构（用于查看表定义）
2. full_backup.sql  - 完整备份（SQL格式，可读）
3. backup.dump      - 压缩备份（推荐用于恢复，最快）

恢复方法:
---------

方法1: 使用压缩备份（推荐，最快）
  pg_restore -U $DB_USER -d $DB_NAME -h localhost -p 5432 \\
    --clean --if-exists \\
    backup.dump

方法2: 使用 SQL 文件
  psql -U $DB_USER -d $DB_NAME -h localhost -p 5432 \\
    -f full_backup.sql

方法3: 从头创建（如果数据库不存在）
  # 1. 创建数据库
  createdb -U $DB_USER $DB_NAME

  # 2. 恢复数据
  pg_restore -U $DB_USER -d $DB_NAME backup.dump
  # 或
  psql -U $DB_USER -d $DB_NAME -f full_backup.sql

重要说明:
---------
- 备份不包含用户权限（--no-owner --no-privileges）
- 恢复时会使用当前登录用户作为所有者
- 如果目标数据库已有数据，使用 --clean 会先删除
- 建议在恢复前先测试连接：psql -U $DB_USER -l

故障排除:
---------
如果恢复失败：
1. 确认 PostgreSQL 版本兼容（source >= target）
2. 确认用户有创建表的权限
3. 如果有外键冲突，可以临时禁用：
   SET session_replication_role = replica;
   -- 导入数据
   SET session_replication_role = DEFAULT;

查看 PostgreSQL 版本:
  psql -U $DB_USER -d $DB_NAME -c "SELECT version();"

EOF

# 显示摘要
echo -e "\n${GREEN}===========================================${NC}"
echo -e "${GREEN}PostgreSQL 备份完成！${NC}"
echo -e "${GREEN}===========================================${NC}"
echo ""
echo "备份位置: $BACKUP_DIR"
echo ""
echo "备份文件:"
ls -lh $BACKUP_DIR/*.sql $BACKUP_DIR/*.dump 2>/dev/null

echo ""
echo "文件大小:"
du -sh $BACKUP_DIR

echo ""
echo -e "${YELLOW}下一步:${NC}"
echo "1. 将备份目录复制到新机器:"
echo "   scp -r $BACKUP_DIR NEW_COMPUTER:~/"
echo ""
echo "2. 在新机器恢复:"
echo "   cd $(basename $BACKUP_DIR)"
echo "   bash restore_postgres.sh"
echo ""
echo "   或手动恢复:"
echo "   pg_restore -U $DB_USER -d $DB_NAME backup.dump"
echo ""

# 可选：创建恢复脚本
cat > $BACKUP_DIR/restore_postgres.sh <<'RESTORE_SCRIPT'
#!/bin/bash
# PostgreSQL 恢复脚本

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== PostgreSQL 数据库恢复工具 ===${NC}"

# 配置（可以通过环境变量覆盖）
DB_USER="${PG_USER:-postgres}"
DB_NAME="${PG_DB:-test}"
DB_HOST="${PG_HOST:-localhost}"
DB_PORT="${PG_PORT:-5432}"

echo "目标数据库配置:"
echo "  主机: $DB_HOST:$DB_PORT"
echo "  数据库: $DB_NAME"
echo "  用户: $DB_USER"
echo ""

# 检查连接
echo -e "${YELLOW}[1/4] 检查 PostgreSQL 连接...${NC}"
if ! pg_isready -h $DB_HOST -p $DB_PORT > /dev/null 2>&1; then
    echo -e "${RED}错误: PostgreSQL 未运行${NC}"
    echo "请先启动 PostgreSQL"
    exit 1
fi
echo -e "${GREEN}✓ PostgreSQL 运行中${NC}"

# 检查数据库是否存在
echo -e "\n${YELLOW}[2/4] 检查目标数据库...${NC}"
if ! psql -U $DB_USER -h $DB_HOST -p $DB_PORT -lqt | cut -d \| -f 1 | grep -qw $DB_NAME; then
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
    echo -e "${YELLOW}警告: 数据库 '$DB_NAME' 已存在${NC}"
    echo "恢复将覆盖现有数据"
    read -p "继续？(y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "已取消"
        exit 0
    fi
fi

# 选择恢复文件
echo -e "\n${YELLOW}[3/4] 选择恢复方式...${NC}"
if [ -f "backup.dump" ]; then
    echo "使用压缩备份: backup.dump（推荐）"
    RESTORE_FILE="backup.dump"
    RESTORE_CMD="pg_restore"
elif [ -f "full_backup.sql" ]; then
    echo "使用 SQL 备份: full_backup.sql"
    RESTORE_FILE="full_backup.sql"
    RESTORE_CMD="psql"
else
    echo -e "${RED}错误: 未找到备份文件${NC}"
    echo "请确保 backup.dump 或 full_backup.sql 存在"
    exit 1
fi

# 恢复数据
echo -e "\n${YELLOW}[4/4] 恢复数据...${NC}"
if [ "$RESTORE_CMD" = "pg_restore" ]; then
    pg_restore -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT \
        --clean \
        --if-exists \
        --no-owner \
        --no-privileges \
        --verbose \
        $RESTORE_FILE
else
    psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT \
        -f $RESTORE_FILE
fi

echo -e "\n${GREEN}===========================================${NC}"
echo -e "${GREEN}PostgreSQL 恢复完成！${NC}"
echo -e "${GREEN}===========================================${NC}"

# 验证
echo -e "\n${YELLOW}验证恢复结果:${NC}"
psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -c "
  SELECT
    schemaname,
    COUNT(*) as table_count
  FROM pg_tables
  WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  GROUP BY schemaname;
"

echo ""
echo -e "${YELLOW}表列表:${NC}"
psql -U $DB_USER -d $DB_NAME -h $DB_HOST -p $DB_PORT -c "\dt core.*"

echo ""
echo -e "${GREEN}恢复完成！${NC}"
RESTORE_SCRIPT

chmod +x $BACKUP_DIR/restore_postgres.sh

echo -e "${GREEN}已生成恢复脚本: restore_postgres.sh${NC}"
echo ""
echo -e "${YELLOW}提示:${NC}"
echo "- 备份采用 --no-owner --no-privileges，恢复后所有表属于当前用户"
echo "- 如需保留原始权限，去掉这两个参数重新备份"
echo ""
