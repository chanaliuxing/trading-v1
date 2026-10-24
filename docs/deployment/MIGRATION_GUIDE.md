# 项目迁移指南

## 概述
将交易系统从当前 Mac 迁移到新电脑的完整策略。

## 服务分类与迁移策略

### 1. 需要数据迁移的服务（关键）

#### ✅ **ClickHouse** - 必须迁移数据
**原因**: 存储所有历史交易数据、市场数据（bars_1m、bars_5m等）

**迁移方案**:
```bash
# 当前机器 - 导出数据
/opt/homebrew/bin/clickhouse client --query "SHOW DATABASES"

# 方案A: 使用 clickhouse-backup（推荐）
brew install clickhouse-backup
clickhouse-backup create migration_backup
clickhouse-backup upload migration_backup

# 方案B: 手动导出
/opt/homebrew/bin/clickhouse client --query "SELECT * FROM trading.bars_1m_ibkr FORMAT Native" > bars_1m.native

# 压缩并复制到新机器
tar -czf clickhouse_data.tar.gz /opt/homebrew/var/lib/clickhouse/
```

**新机器 - Docker方案**:
```bash
# 使用 infra/compose.yml 中的配置
docker-compose -f infra/compose.yml up -d clickhouse

# 恢复数据
docker exec -i trading_clickhouse clickhouse-client < backup.sql
```

**数据位置**:
- 当前: `/opt/homebrew/var/lib/clickhouse/`
- Docker: 使用命名卷 `ch_data`（推荐）

---

#### ⚠️ **PostgreSQL** - 如果有数据需迁移
**检查是否使用**:
```bash
# 检查是否有数据
psql -U postgres -d test -c "\dt"
```

**迁移方案**:
```bash
# 导出
pg_dump -U postgres test > postgres_backup.sql

# 新机器导入（Docker）
docker-compose -f infra/compose.yml up -d postgres
docker exec -i trading_postgres psql -U postgres test < postgres_backup.sql
```

---

### 2. 可选迁移的服务

#### 🔄 **Kafka** - 通常不需要迁移数据
**原因**:
- 消息队列通常不保留历史数据
- 你的配置显示保留时间只有 168 小时（7天）
- 新环境启动后会自动创建新消息

**建议**: **不迁移数据，在新机器直接启动**

**新机器配置**:
```bash
# 使用 Docker Compose
docker-compose -f infra/compose.yml up -d zookeeper kafka schema-registry
```

**如果确实需要迁移数据**:
```bash
# 当前机器 - 复制数据目录
tar -czf kafka_data.tar.gz /opt/homebrew/var/lib/kafka/

# 新机器 - 解压到 Docker 卷
```

---

#### ♻️ **Redis** - 状态缓存，可选迁移
**原因**: 主要用于状态总线（statebus），重启后会重建

**建议**: **不迁移，直接使用 Docker**

**如需迁移**:
```bash
# 当前机器
redis-cli SAVE
cp /opt/homebrew/var/db/redis/dump.rdb ~/redis_backup.rdb

# 新机器
docker-compose -f infra/compose.yml up -d redis
docker cp redis_backup.rdb trading_redis:/data/dump.rdb
docker restart trading_redis
```

---

### 3. 完全不需要迁移的服务（推荐 Docker）

#### ✨ **可观测性栈** - Grafana, Loki, Promtail, Tempo, Prometheus

**原因**:
- 配置文件已经在项目中管理
- 历史日志和追踪数据通常不需要保留
- Dashboard 配置在 `grafana/dashboards/` 目录

**新机器方案**:
```bash
# 方案1: 使用项目根目录的 docker-compose.yml
docker-compose up -d grafana loki promtail tempo

# 方案2: 使用 infra/compose.yml（更完整）
docker-compose -f infra/compose.yml up -d prometheus grafana
```

**Grafana配置迁移**:
- Dashboard: 已在 `grafana/dashboards/*.json`
- 数据源配置: 在 `grafana/provisioning/datasources/ds.yml`
- 直接复制项目文件即可

---

## 完整迁移步骤

### 第一步: 准备数据备份（当前机器）

```bash
cd /workspace/Documents/trading

# 1. 备份 ClickHouse（最重要！）
mkdir -p ~/trading_migration/clickhouse
/opt/homebrew/bin/clickhouse client --query "
  SELECT database, name, engine, total_rows, total_bytes
  FROM system.tables
  WHERE database = 'trading'
" > ~/trading_migration/clickhouse_tables.txt

# 导出所有表
/opt/homebrew/bin/clickhouse client --query "
  SELECT name FROM system.tables WHERE database = 'trading'
" | while read table; do
  /opt/homebrew/bin/clickhouse client --query "
    SELECT * FROM trading.$table FORMAT Native
  " > ~/trading_migration/clickhouse/${table}.native
done

# 2. 检查并备份 PostgreSQL（如果使用）
pg_dump -U postgres test > ~/trading_migration/postgres.sql 2>/dev/null || echo "No PostgreSQL"

# 3. 备份项目文件
cd ~/Documents
tar --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='logs' \
    --exclude='*.pyc' \
    -czf ~/trading_migration/trading_code.tar.gz trading/

# 4. 导出环境变量
cp ~/Documents/trading/.env ~/trading_migration/.env
```

### 第二步: 传输到新机器

```bash
# 方案A: 使用外部硬盘
cp -r ~/trading_migration /Volumes/YOUR_DRIVE/

# 方案B: 使用网络传输
rsync -avz ~/trading_migration/ NEW_COMPUTER_IP:~/trading_migration/

# 方案C: 云存储
# 上传到 S3/Google Drive/Dropbox
```

### 第三步: 新机器安装（推荐 Docker）

```bash
# 1. 安装 Docker Desktop
# 下载: https://www.docker.com/products/docker-desktop

# 2. 恢复项目代码
cd ~
tar -xzf ~/trading_migration/trading_code.tar.gz
cd trading

# 3. 恢复环境变量
cp ~/trading_migration/.env .

# 4. 创建 Python 虚拟环境
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt  # 如果有

# 5. 启动所有基础设施（Docker）
docker-compose -f infra/compose.yml up -d

# 6. 恢复 ClickHouse 数据
for table_file in ~/trading_migration/clickhouse/*.native; do
  table_name=$(basename $table_file .native)
  docker exec -i trading_clickhouse clickhouse-client --query "
    INSERT INTO trading.$table_name FORMAT Native
  " < $table_file
done

# 7. 验证数据
docker exec trading_clickhouse clickhouse-client --query "
  SELECT database, name, total_rows
  FROM system.tables
  WHERE database = 'trading'
"

# 8. 启动可观测性栈
docker-compose up -d
```

### 第四步: 验证迁移

```bash
# 检查所有服务状态
docker-compose -f infra/compose.yml ps

# 测试 ClickHouse
docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM trading.bars_1m_ibkr"

# 测试 Redis
docker exec trading_redis redis-cli ping

# 测试 Kafka
docker exec trading_kafka kafka-topics --list --bootstrap-server localhost:9092

# 访问 Grafana
open http://localhost:3000
# 用户名: admin, 密码: admin
```

---

## Docker vs 本地安装对比

### 推荐使用 Docker 的服务

| 服务 | 原因 | Docker 优势 |
|------|------|------------|
| **Kafka + Zookeeper** | 配置复杂 | 版本管理、快速部署、易于迁移 |
| **Schema Registry** | Kafka 依赖 | 与 Kafka 环境隔离 |
| **Prometheus** | 监控服务 | 无状态，配置文件管理 |
| **Grafana** | 可视化 | Dashboard 配置文件化 |
| **Loki + Promtail** | 日志聚合 | 日志数据不需永久保存 |
| **Tempo** | 分布式追踪 | 临时数据 |
| **Redis** | 缓存 | 可重建状态 |

### 持久化数据处理

| 服务 | 数据持久化方案 | 迁移难度 |
|------|----------------|----------|
| **ClickHouse** | Docker Named Volume `ch_data` | 中等 - 使用 Native 格式导出/导入 |
| **PostgreSQL** | Docker Named Volume `pg_data` | 简单 - 使用 pg_dump/restore |
| **Kafka** | Docker Named Volume `kafka_data` | 复杂 - 但通常不需要 |

---

## 推荐的新机器架构

```yaml
# 完全使用 Docker Compose
# infra/compose.yml 已经配置好了所有服务！

架构:
┌─────────────────────────────────────┐
│ 交易应用 (Python venv)              │
│ - main.py                           │
│ - strategies/                       │
└─────────────────────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│ Docker 基础设施                     │
│                                     │
│ [ClickHouse] ← 交易数据存储         │
│ [PostgreSQL] ← 元数据（如果使用）   │
│ [Kafka + Zookeeper] ← 消息队列      │
│ [Redis] ← 状态缓存                  │
│ [Prometheus] ← 指标收集             │
│ [Grafana] ← 可视化                  │
│ [Loki + Promtail] ← 日志聚合        │
│ [Tempo] ← 分布式追踪                │
└─────────────────────────────────────┘
```

---

## 快速启动脚本

### 当前机器备份脚本

```bash
#!/bin/bash
# save as: backup_for_migration.sh

set -e
BACKUP_DIR=~/trading_migration
mkdir -p $BACKUP_DIR

echo "=== 备份 ClickHouse 数据 ==="
mkdir -p $BACKUP_DIR/clickhouse
/opt/homebrew/bin/clickhouse client --query "
  SELECT name FROM system.tables WHERE database = 'trading'
" | while read table; do
  echo "导出表: $table"
  /opt/homebrew/bin/clickhouse client --query "
    SELECT * FROM trading.$table FORMAT Native
  " > $BACKUP_DIR/clickhouse/${table}.native
done

echo "=== 备份代码 ==="
cd ~/Documents
tar --exclude='venv' --exclude='__pycache__' --exclude='.git' \
    -czf $BACKUP_DIR/trading_code.tar.gz trading/

echo "=== 备份配置 ==="
cp ~/Documents/trading/.env $BACKUP_DIR/.env 2>/dev/null || echo "No .env file"

echo "=== 备份完成 ==="
echo "备份位置: $BACKUP_DIR"
du -sh $BACKUP_DIR
```

### 新机器恢复脚本

```bash
#!/bin/bash
# save as: restore_on_new_machine.sh

set -e
BACKUP_DIR=~/trading_migration
PROJECT_DIR=~/Documents/trading

echo "=== 恢复代码 ==="
mkdir -p ~/Documents
cd ~/Documents
tar -xzf $BACKUP_DIR/trading_code.tar.gz

echo "=== 启动 Docker 服务 ==="
cd $PROJECT_DIR
docker-compose -f infra/compose.yml up -d

# 等待服务启动
echo "等待服务启动..."
sleep 30

echo "=== 恢复 ClickHouse 数据 ==="
for table_file in $BACKUP_DIR/clickhouse/*.native; do
  table_name=$(basename $table_file .native)
  echo "导入表: $table_name"
  docker exec -i trading_clickhouse clickhouse-client --query "
    INSERT INTO trading.$table_name FORMAT Native
  " < $table_file || echo "表 $table_name 导入失败或不存在"
done

echo "=== 验证迁移 ==="
docker-compose -f infra/compose.yml ps
docker exec trading_clickhouse clickhouse-client --query "SHOW TABLES FROM trading"

echo "=== 迁移完成 ==="
echo "访问 Grafana: http://localhost:3000 (admin/admin)"
echo "ClickHouse HTTP: http://localhost:8123"
echo "Kafka: localhost:9092"
```

---

## 常见问题

### Q1: ClickHouse 数据能用 Docker 吗？
**A**: 可以！使用 Docker Named Volume 确保数据持久化：
```yaml
volumes:
  - ch_data:/var/lib/clickhouse  # 数据安全存储
```

### Q2: 为什么不建议迁移 Kafka 数据？
**A**:
- Kafka 设计为流式处理，历史消息通常不重要
- 你的配置保留期只有 7 天
- 重新启动后，新消息会自动产生

### Q3: Grafana Dashboard 会丢失吗？
**A**: 不会！你的配置已经在代码中：
- `grafana/dashboards/*.json`
- `grafana/provisioning/datasources/ds.yml`

### Q4: 如何确保不丢失重要数据？
**A**: 只有这些需要备份：
1. ✅ ClickHouse 数据库（bars_*）
2. ✅ PostgreSQL（如果使用）
3. ✅ 项目代码和配置文件
4. ✅ .env 环境变量

其他都可以重建！

---

## 时间估算

| 步骤 | 预计时间 |
|------|----------|
| 备份 ClickHouse 数据 | 10-60分钟（取决于数据量） |
| 复制文件到新机器 | 30-120分钟（取决于传输方式） |
| 新机器安装 Docker | 10分钟 |
| 启动所有服务 | 5分钟 |
| 恢复 ClickHouse 数据 | 10-60分钟 |
| 验证和调试 | 30分钟 |
| **总计** | **2-5小时** |

---

## 迁移检查清单

### 迁移前
- [ ] 检查 ClickHouse 数据量：`clickhouse-client --query "SELECT database, sum(total_bytes) FROM system.tables WHERE database = 'trading' GROUP BY database"`
- [ ] 确认所有交易策略配置文件已提交到 git
- [ ] 导出 .env 环境变量
- [ ] 记录当前服务版本

### 迁移中
- [ ] 备份 ClickHouse 完成
- [ ] 代码打包完成
- [ ] 文件传输到新机器
- [ ] Docker 安装完成

### 迁移后
- [ ] 所有 Docker 容器运行正常
- [ ] ClickHouse 数据量一致
- [ ] Grafana 可访问
- [ ] Kafka topics 创建成功
- [ ] Python 应用连接测试
- [ ] 运行一次测试策略

---

## 联系方式
如有问题，参考：
- ClickHouse 文档: https://clickhouse.com/docs
- Docker Compose 文档: https://docs.docker.com/compose/
- Kafka 文档: https://kafka.apache.org/documentation/
