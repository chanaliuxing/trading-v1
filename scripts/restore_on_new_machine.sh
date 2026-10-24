#!/bin/bash
# 迁移恢复脚本 - 在新机器运行
# 用途: 恢复所有备份数据并启动 Docker 服务

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== 交易系统迁移恢复工具 ===${NC}"

# 检查 Docker
echo -e "\n${YELLOW}[检查] 验证 Docker 安装...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${RED}错误: Docker 未安装${NC}"
    echo "请先安装 Docker Desktop: https://www.docker.com/products/docker-desktop"
    exit 1
fi

if ! docker info &> /dev/null; then
    echo -e "${RED}错误: Docker 未运行${NC}"
    echo "请启动 Docker Desktop"
    exit 1
fi

echo -e "${GREEN}✓ Docker 已就绪${NC}"

# 查找备份目录
echo -e "\n${YELLOW}[检查] 查找备份目录...${NC}"
BACKUP_DIRS=$(find ~ -maxdepth 1 -type d -name "trading_migration_*" 2>/dev/null | sort -r)

if [ -z "$BACKUP_DIRS" ]; then
    echo -e "${RED}错误: 未找到备份目录${NC}"
    echo "请确保备份目录在 ~/ 下，格式为 trading_migration_YYYYMMDD_HHMMSS"
    echo ""
    echo "或手动指定备份目录:"
    echo "  export BACKUP_DIR=/path/to/backup"
    echo "  bash $0"
    exit 1
fi

# 选择备份目录（如果有多个）
BACKUP_DIR=$(echo "$BACKUP_DIRS" | head -1)

if [ ! -z "$BACKUP_DIR" ]; then
    echo "找到备份目录:"
    echo "$BACKUP_DIRS" | nl
    echo ""
    echo -e "使用最新备份: ${GREEN}$BACKUP_DIR${NC}"
    read -p "继续？(y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "已取消"
        exit 0
    fi
fi

PROJECT_DIR=~/Documents/trading

# 1. 恢复项目代码
echo -e "\n${YELLOW}[1/7] 恢复项目代码...${NC}"
mkdir -p ~/Documents
cd ~/Documents

if [ -d "trading" ]; then
    echo -e "${YELLOW}警告: trading 目录已存在${NC}"
    read -p "是否覆盖？(y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        mv trading trading.backup.$(date +%Y%m%d_%H%M%S)
        echo "已备份到 trading.backup.*"
    else
        echo "跳过代码恢复"
        PROJECT_DIR=~/Documents/trading
    fi
fi

if [ ! -d "$PROJECT_DIR" ]; then
    tar -xzf $BACKUP_DIR/code/trading_code.tar.gz
    echo -e "${GREEN}✓ 代码恢复完成${NC}"
fi

cd $PROJECT_DIR

# 2. 恢复配置文件
echo -e "\n${YELLOW}[2/7] 恢复配置文件...${NC}"
cp $BACKUP_DIR/config/.env .env 2>/dev/null || echo "无 .env 文件"
cp $BACKUP_DIR/config/config.yml config.yml
echo -e "${GREEN}✓ 配置恢复完成${NC}"

# 3. 创建 Python 虚拟环境
echo -e "\n${YELLOW}[3/7] 设置 Python 环境...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "创建虚拟环境..."
fi

source venv/bin/activate

if [ -f "requirements.txt" ]; then
    echo "安装 Python 依赖（这可能需要几分钟）..."
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    echo -e "${GREEN}✓ Python 依赖安装完成${NC}"
else
    echo -e "${YELLOW}! 未找到 requirements.txt，跳过依赖安装${NC}"
fi

# 4. 启动 Docker 服务
echo -e "\n${YELLOW}[4/7] 启动 Docker 基础设施...${NC}"
docker-compose -f infra/compose.yml up -d

echo "等待服务启动（30秒）..."
sleep 10
echo "20秒..."
sleep 10
echo "10秒..."
sleep 10

# 检查服务状态
docker-compose -f infra/compose.yml ps

echo -e "${GREEN}✓ Docker 服务已启动${NC}"

# 5. 恢复 ClickHouse 数据
echo -e "\n${YELLOW}[5/7] 恢复 ClickHouse 数据...${NC}"

# 首先创建数据库
echo "创建数据库..."
docker exec trading_clickhouse clickhouse-client --query "
  CREATE DATABASE IF NOT EXISTS trading
"

# 恢复表结构
echo "恢复表结构..."
for schema_file in $BACKUP_DIR/clickhouse/*_schema.sql; do
    if [ -f "$schema_file" ]; then
        table_name=$(basename $schema_file _schema.sql)
        echo "  - 创建表: $table_name"
        docker exec -i trading_clickhouse clickhouse-client < $schema_file || echo "表已存在或创建失败"
    fi
done

# 恢复数据
echo "恢复表数据（这可能需要一些时间）..."
for table_file in $BACKUP_DIR/clickhouse/*.native; do
    if [ -f "$table_file" ]; then
        table_name=$(basename $table_file .native)
        echo "  - 导入数据: $table_name"
        docker exec -i trading_clickhouse clickhouse-client --query "
          INSERT INTO trading.$table_name FORMAT Native
        " < $table_file || echo "数据导入失败: $table_name"
    fi
done

echo -e "${GREEN}✓ ClickHouse 数据恢复完成${NC}"

# 6. PostgreSQL 恢复提示（使用本地安装）
echo -e "\n${YELLOW}[6/7] PostgreSQL 恢复...${NC}"
echo -e "${YELLOW}注意: PostgreSQL 使用本地安装（不是 Docker）${NC}"

if [ -d "$BACKUP_DIR/postgres_backup" ]; then
    echo "发现 PostgreSQL 备份: $BACKUP_DIR/postgres_backup"
    echo ""
    echo "请使用专用脚本恢复 PostgreSQL："
    echo "  cd $BACKUP_DIR/postgres_backup"
    echo "  bash restore_postgres.sh"
    echo ""
    echo "或稍后手动恢复："
    echo "  pg_restore -U postgres -d test backup.dump"
    echo ""
    read -p "是否现在运行 PostgreSQL 恢复脚本？(y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        cd $BACKUP_DIR/postgres_backup
        if [ -f "restore_postgres.sh" ]; then
            bash restore_postgres.sh
            cd $PROJECT_DIR
            echo -e "${GREEN}✓ PostgreSQL 恢复完成${NC}"
        else
            echo -e "${YELLOW}! 未找到恢复脚本，请手动恢复${NC}"
            cd $PROJECT_DIR
        fi
    else
        echo -e "${YELLOW}跳过 PostgreSQL 恢复${NC}"
    fi
else
    echo "无 PostgreSQL 备份，跳过"
    echo "如需恢复 PostgreSQL，请参考 scripts/restore_postgres.sh"
fi

# 7. 恢复 Redis（如果存在）
echo -e "\n${YELLOW}[7/7] 检查 Redis 备份...${NC}"
if [ -f "$BACKUP_DIR/redis_dump.rdb" ]; then
    echo "恢复 Redis 数据..."
    docker cp $BACKUP_DIR/redis_dump.rdb trading_redis:/data/dump.rdb
    docker restart trading_redis
    sleep 5
    echo -e "${GREEN}✓ Redis 恢复完成${NC}"
else
    echo "无 Redis 备份，跳过"
fi

# 验证恢复
echo -e "\n${BLUE}===========================================${NC}"
echo -e "${BLUE}验证恢复结果${NC}"
echo -e "${BLUE}===========================================${NC}"

echo -e "\n${YELLOW}Docker 服务状态:${NC}"
docker-compose -f infra/compose.yml ps

echo -e "\n${YELLOW}ClickHouse 数据库:${NC}"
docker exec trading_clickhouse clickhouse-client --query "
  SELECT
    name as table_name,
    formatReadableSize(total_bytes) as size,
    formatReadableQuantity(total_rows) as rows
  FROM system.tables
  WHERE database = 'trading'
  ORDER BY total_bytes DESC
" || echo "查询失败"

echo -e "\n${YELLOW}Kafka Topics:${NC}"
docker exec trading_kafka kafka-topics --list --bootstrap-server localhost:9092 2>/dev/null || echo "Kafka 未就绪"

echo -e "\n${YELLOW}Redis 状态:${NC}"
docker exec trading_redis redis-cli ping || echo "Redis 未就绪"

# 完成
echo -e "\n${GREEN}===========================================${NC}"
echo -e "${GREEN}迁移恢复完成！${NC}"
echo -e "${GREEN}===========================================${NC}"

echo -e "\n${YELLOW}服务访问地址:${NC}"
echo "  ClickHouse HTTP: http://localhost:8123"
echo "  ClickHouse Native: localhost:9000"
echo "  Grafana: http://localhost:3000 (admin/admin)"
echo "  Prometheus: http://localhost:9090"
echo "  Kafka: localhost:9092"
echo "  Redis: localhost:6379"
echo "  PostgreSQL: localhost:5432"

echo -e "\n${YELLOW}下一步:${NC}"
echo "1. 访问 Grafana 验证 Dashboard: http://localhost:3000"
echo "2. 检查 ClickHouse 数据:"
echo "   docker exec -it trading_clickhouse clickhouse-client"
echo "   SELECT count() FROM trading.bars_1m_ibkr;"
echo ""
echo "3. 激活 Python 环境并运行应用:"
echo "   cd $PROJECT_DIR"
echo "   source venv/bin/activate"
echo "   python main.py  # 或你的启动脚本"
echo ""
echo "4. 查看服务日志:"
echo "   docker-compose -f infra/compose.yml logs -f [service_name]"

echo -e "\n${YELLOW}故障排除:${NC}"
echo "- 服务未启动: docker-compose -f infra/compose.yml restart"
echo "- 查看错误日志: docker-compose -f infra/compose.yml logs"
echo "- 重新启动所有服务: docker-compose -f infra/compose.yml down && docker-compose -f infra/compose.yml up -d"

echo -e "\n${GREEN}恢复流程全部完成！${NC}"
