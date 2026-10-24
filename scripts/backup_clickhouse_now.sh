#!/bin/bash
# ClickHouse 即时备份脚本

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=~/clickhouse_backup_${TIMESTAMP}

echo -e "${GREEN}=== ClickHouse 备份工具 ===${NC}"
echo "备份目录: $BACKUP_DIR"

# 创建目录
mkdir -p $BACKUP_DIR/schemas
mkdir -p $BACKUP_DIR/data

# 1. 导出表列表
echo -e "\n${YELLOW}[1/3] 获取表列表...${NC}"
/opt/homebrew/bin/clickhouse client --query "
  SELECT name
  FROM system.tables
  WHERE database = 'trading'
    AND engine NOT LIKE 'Materialized%'
    AND engine NOT IN ('View', 'MaterializedView')
  ORDER BY name
" > $BACKUP_DIR/tables.txt

TABLE_COUNT=$(wc -l < $BACKUP_DIR/tables.txt | tr -d ' ')
echo "找到 $TABLE_COUNT 张表"

# 2. 导出表结构
echo -e "\n${YELLOW}[2/3] 导出表结构...${NC}"
while IFS= read -r table; do
  if [ ! -z "$table" ]; then
    echo "  - 导出结构: $table"
    /opt/homebrew/bin/clickhouse client --query "SHOW CREATE TABLE trading.$table" > $BACKUP_DIR/schemas/${table}_schema.sql 2>/dev/null || echo "    (跳过)"
  fi
done < $BACKUP_DIR/tables.txt

# 3. 导出有数据的表
echo -e "\n${YELLOW}[3/3] 导出表数据...${NC}"
while IFS= read -r table; do
  if [ ! -z "$table" ]; then
    # 检查行数
    row_count=$(/opt/homebrew/bin/clickhouse client --query "SELECT count() FROM trading.$table" 2>/dev/null || echo "0")

    if [ "$row_count" -gt 0 ]; then
      size=$(/opt/homebrew/bin/clickhouse client --query "
        SELECT formatReadableSize(total_bytes)
        FROM system.tables
        WHERE database = 'trading' AND name = '$table'
      " 2>/dev/null || echo "0")

      echo "  - 导出数据: $table ($row_count 行, $size)"
      /opt/homebrew/bin/clickhouse client --query "SELECT * FROM trading.$table FORMAT Native" > $BACKUP_DIR/data/${table}.native 2>/dev/null || echo "    (失败)"
    else
      echo "  - 跳过空表: $table"
    fi
  fi
done < $BACKUP_DIR/tables.txt

# 生成统计信息
echo -e "\n${YELLOW}生成统计信息...${NC}"
/opt/homebrew/bin/clickhouse client --query "
  SELECT
    name,
    formatReadableSize(total_bytes) as size,
    formatReadableQuantity(total_rows) as rows
  FROM system.tables
  WHERE database = 'trading' AND total_rows > 0
  ORDER BY total_bytes DESC
" > $BACKUP_DIR/stats.txt

# 生成恢复脚本
cat > $BACKUP_DIR/restore.sh <<'RESTORE'
#!/bin/bash
# ClickHouse 恢复脚本

set -e
BACKUP_DIR=$(dirname "$0")

echo "=== ClickHouse 恢复工具 ==="
echo "备份目录: $BACKUP_DIR"

# 检查 ClickHouse
if ! /opt/homebrew/bin/clickhouse client --query "SELECT 1" > /dev/null 2>&1; then
  echo "错误: ClickHouse 未运行"
  echo "请先启动: brew services start clickhouse"
  exit 1
fi

# 创建数据库
echo "创建数据库..."
/opt/homebrew/bin/clickhouse client --query "CREATE DATABASE IF NOT EXISTS trading"

# 恢复表结构
echo "恢复表结构..."
for schema_file in $BACKUP_DIR/schemas/*_schema.sql; do
  if [ -f "$schema_file" ]; then
    table_name=$(basename $schema_file _schema.sql)
    echo "  - 创建表: $table_name"
    /opt/homebrew/bin/clickhouse client < $schema_file 2>/dev/null || echo "    (已存在或失败)"
  fi
done

# 恢复数据
echo "恢复表数据..."
for data_file in $BACKUP_DIR/data/*.native; do
  if [ -f "$data_file" ]; then
    table_name=$(basename $data_file .native)
    echo "  - 导入数据: $table_name"
    /opt/homebrew/bin/clickhouse client --query "INSERT INTO trading.$table_name FORMAT Native" < $data_file 2>/dev/null || echo "    (失败)"
  fi
done

echo ""
echo "=== 恢复完成 ==="
echo "验证数据:"
/opt/homebrew/bin/clickhouse client --query "
  SELECT
    name,
    formatReadableSize(total_bytes) as size,
    total_rows as rows
  FROM system.tables
  WHERE database = 'trading' AND total_rows > 0
  ORDER BY total_bytes DESC
  LIMIT 10
"
RESTORE

chmod +x $BACKUP_DIR/restore.sh

# 显示摘要
echo -e "\n${GREEN}===========================================${NC}"
echo -e "${GREEN}备份完成！${NC}"
echo -e "${GREEN}===========================================${NC}"
echo ""
echo "备份位置: $BACKUP_DIR"
echo ""
echo "备份内容:"
echo "  - 表结构: $(ls $BACKUP_DIR/schemas/*.sql 2>/dev/null | wc -l | tr -d ' ') 个文件"
echo "  - 数据文件: $(ls $BACKUP_DIR/data/*.native 2>/dev/null | wc -l | tr -d ' ') 个文件"
echo ""
echo "备份大小:"
du -sh $BACKUP_DIR
echo ""
echo "数据统计:"
head -20 $BACKUP_DIR/stats.txt
echo ""
echo -e "${YELLOW}恢复方法:${NC}"
echo "  cd $BACKUP_DIR"
echo "  bash restore.sh"
echo ""
