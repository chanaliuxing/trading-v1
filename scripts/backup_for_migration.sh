#!/bin/bash
# 迁移备份脚本 - 在当前机器运行
# 用途: 备份所有需要迁移的数据和配置

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

BACKUP_DIR=~/trading_migration
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUP_DIR}_${TIMESTAMP}"

echo -e "${GREEN}=== 交易系统迁移备份工具 ===${NC}"
echo "备份目录: $BACKUP_DIR"

mkdir -p $BACKUP_DIR/{clickhouse,config,code}

# 1. 备份 ClickHouse 数据
echo -e "\n${YELLOW}[1/6] 备份 ClickHouse 数据库...${NC}"
mkdir -p $BACKUP_DIR/clickhouse

# 获取所有表列表
echo "获取表列表..."
/opt/homebrew/bin/clickhouse client --query "
  SELECT name FROM system.tables
  WHERE database = 'trading'
  ORDER BY name
" > $BACKUP_DIR/clickhouse/tables.txt

# 统计数据量
/opt/homebrew/bin/clickhouse client --query "
  SELECT
    name as table_name,
    formatReadableSize(total_bytes) as size,
    formatReadableQuantity(total_rows) as rows
  FROM system.tables
  WHERE database = 'trading'
  ORDER BY total_bytes DESC
" > $BACKUP_DIR/clickhouse/stats.txt

echo "数据库统计信息:"
cat $BACKUP_DIR/clickhouse/stats.txt

# 导出表结构
echo "导出表结构..."
/opt/homebrew/bin/clickhouse client --query "
  SELECT name FROM system.tables WHERE database = 'trading'
" | while read table; do
  echo "  - 导出表结构: $table"
  /opt/homebrew/bin/clickhouse client --query "
    SHOW CREATE TABLE trading.$table
  " > $BACKUP_DIR/clickhouse/${table}_schema.sql
done

# 导出数据（Native 格式最快且保留类型）
echo "导出表数据（这可能需要一些时间）..."
/opt/homebrew/bin/clickhouse client --query "
  SELECT name FROM system.tables WHERE database = 'trading'
" | while read table; do
  row_count=$(/opt/homebrew/bin/clickhouse client --query "SELECT count() FROM trading.$table")
  if [ "$row_count" -gt 0 ]; then
    echo "  - 导出数据: $table ($row_count 行)"
    /opt/homebrew/bin/clickhouse client --query "
      SELECT * FROM trading.$table FORMAT Native
    " > $BACKUP_DIR/clickhouse/${table}.native
  else
    echo "  - 跳过空表: $table"
  fi
done

echo -e "${GREEN}✓ ClickHouse 备份完成${NC}"

# 2. PostgreSQL 备份提示（使用独立脚本）
echo -e "\n${YELLOW}[2/6] PostgreSQL 备份...${NC}"
echo -e "${YELLOW}注意: PostgreSQL 使用本地安装（不是 Docker）${NC}"
echo "请使用专用脚本备份 PostgreSQL："
echo "  bash scripts/backup_postgres.sh"
echo ""
read -p "是否现在运行 PostgreSQL 备份脚本？(y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  if [ -f "scripts/backup_postgres.sh" ]; then
    bash scripts/backup_postgres.sh
    # 复制 PostgreSQL 备份到主备份目录
    LATEST_PG_BACKUP=$(find ~ -maxdepth 1 -type d -name "postgres_backup_*" | sort -r | head -1)
    if [ ! -z "$LATEST_PG_BACKUP" ]; then
      cp -r "$LATEST_PG_BACKUP" $BACKUP_DIR/postgres_backup
      echo -e "${GREEN}✓ PostgreSQL 备份已复制到主备份目录${NC}"
    fi
  else
    echo -e "${YELLOW}! 未找到 backup_postgres.sh 脚本${NC}"
    echo "请手动运行: bash scripts/backup_postgres.sh"
  fi
else
  echo -e "${YELLOW}跳过 PostgreSQL 备份${NC}"
  echo "稍后可单独运行: bash scripts/backup_postgres.sh"
fi

# 3. 备份 Redis 数据（可选）
echo -e "\n${YELLOW}[3/6] 检查 Redis...${NC}"
if command -v redis-cli &> /dev/null; then
  if redis-cli ping &> /dev/null; then
    echo "备份 Redis 数据..."
    redis-cli SAVE
    cp /opt/homebrew/var/db/redis/dump.rdb $BACKUP_DIR/redis_dump.rdb 2>&1 || echo "Redis 备份失败"
    echo -e "${GREEN}✓ Redis 备份完成${NC}"
  else
    echo "Redis 未运行，跳过"
  fi
else
  echo "未安装 Redis，跳过"
fi

# 4. 备份项目代码
echo -e "\n${YELLOW}[4/6] 备份项目代码...${NC}"
cd ~/Documents
tar --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='logs' \
    --exclude='*.pyc' \
    --exclude='*.log' \
    --exclude='.pytest_cache' \
    --exclude='.mypy_cache' \
    --exclude='node_modules' \
    -czf $BACKUP_DIR/code/trading_code.tar.gz trading/

echo -e "${GREEN}✓ 代码备份完成${NC}"

# 5. 备份配置文件
echo -e "\n${YELLOW}[5/6] 备份配置文件...${NC}"
cd ~/Documents/trading

# 环境变量
if [ -f .env ]; then
  cp .env $BACKUP_DIR/config/.env
  echo "  ✓ .env"
else
  echo "  ! .env 文件不存在"
fi

# 主配置
cp config.yml $BACKUP_DIR/config/config.yml
echo "  ✓ config.yml"

# Docker Compose 配置
cp infra/compose.yml $BACKUP_DIR/config/compose.yml
cp docker-compose.yml $BACKUP_DIR/config/docker-compose.yml 2>/dev/null || true
echo "  ✓ Docker Compose 配置"

# Grafana dashboards
cp -r grafana/dashboards $BACKUP_DIR/config/grafana_dashboards
echo "  ✓ Grafana dashboards"

# 策略配置
cp -r strategies/configs $BACKUP_DIR/config/strategy_configs 2>/dev/null || true
echo "  ✓ 策略配置"

echo -e "${GREEN}✓ 配置文件备份完成${NC}"

# 6. 生成恢复说明
echo -e "\n${YELLOW}[6/6] 生成恢复说明...${NC}"
cat > $BACKUP_DIR/README.txt <<EOF
==========================================
交易系统备份 - $(date)
==========================================

备份内容:
1. ClickHouse 数据库
   - 位置: clickhouse/
   - 格式: Native (高效二进制格式)
   - 表结构: *_schema.sql
   - 数据文件: *.native

2. 项目代码
   - 位置: code/trading_code.tar.gz
   - 已排除: venv, __pycache__, .git, logs

3. 配置文件
   - 位置: config/
   - 包含: .env, config.yml, docker-compose.yml, dashboards

4. PostgreSQL (如果存在)
   - 位置: postgres.sql

5. Redis (如果存在)
   - 位置: redis_dump.rdb

恢复步骤:
-----------
1. 将此备份目录复制到新机器
2. 运行 restore_on_new_machine.sh 脚本
3. 或参考 MIGRATION_GUIDE.md 手动恢复

重要提示:
-----------
- 确保新机器已安装 Docker Desktop
- 建议使用完全容器化部署（infra/compose.yml）
- ClickHouse 数据最重要，其他服务可重建

备份统计:
-----------
EOF

# 添加备份大小信息
du -sh $BACKUP_DIR >> $BACKUP_DIR/README.txt
du -sh $BACKUP_DIR/* >> $BACKUP_DIR/README.txt

echo -e "${GREEN}✓ 恢复说明生成完成${NC}"

# 显示备份摘要
echo -e "\n${GREEN}===========================================${NC}"
echo -e "${GREEN}备份完成！${NC}"
echo -e "${GREEN}===========================================${NC}"
echo ""
echo "备份位置: $BACKUP_DIR"
echo ""
echo "备份大小:"
du -sh $BACKUP_DIR
echo ""
echo "目录结构:"
tree -L 2 $BACKUP_DIR 2>/dev/null || find $BACKUP_DIR -maxdepth 2 -type d

echo ""
echo -e "${YELLOW}下一步:${NC}"
echo "1. 将备份目录复制到新机器"
echo "   方式A: 外部硬盘"
echo "   方式B: rsync -avz $BACKUP_DIR NEW_COMPUTER:~/"
echo "   方式C: 压缩后上传云存储"
echo ""
echo "2. 在新机器运行恢复脚本:"
echo "   bash restore_on_new_machine.sh"
echo ""
echo -e "${YELLOW}建议:${NC}"
echo "- 压缩备份以节省空间和传输时间:"
echo "  tar -czf trading_backup_${TIMESTAMP}.tar.gz $BACKUP_DIR"
echo ""

# 可选：自动压缩
read -p "是否现在压缩备份？(y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  echo "压缩中..."
  cd ~
  tar -czf trading_backup_${TIMESTAMP}.tar.gz $(basename $BACKUP_DIR)
  echo -e "${GREEN}压缩完成: ~/trading_backup_${TIMESTAMP}.tar.gz${NC}"
  ls -lh ~/trading_backup_${TIMESTAMP}.tar.gz
fi

echo -e "\n${GREEN}备份流程全部完成！${NC}"
