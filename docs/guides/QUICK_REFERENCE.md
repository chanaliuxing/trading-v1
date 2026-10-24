# 快速参考卡

## 🎯 核心概念一句话

```
PostgreSQL: 本地安装（已有，直接用）
其他服务: Docker（一键启动）
数据迁移: 自动化脚本
```

---

## ⚡ 关键命令

### 当前机器（备份）

```bash
cd ~/Documents/trading

# 备份所有（推荐）
bash scripts/backup_for_migration.sh

# 或分别备份
bash scripts/backup_for_migration.sh    # ClickHouse + 代码
bash scripts/backup_postgres.sh         # PostgreSQL

# 压缩备份（便于传输）
tar -czf trading_backup.tar.gz ~/trading_migration_*
```

### 新机器（恢复）

```bash
# 1. 解压备份
tar -xzf trading_backup.tar.gz

# 2. 恢复所有（推荐）
bash scripts/restore_on_new_machine.sh

# 3. PostgreSQL 会交互式提示恢复
# 或手动恢复:
cd ~/postgres_backup_*/
bash restore_postgres.sh
```

---

## 📋 服务端口速查

| 服务 | 端口 | 访问 |
|------|------|------|
| **PostgreSQL** | 5432 | `psql -U postgres -d test` |
| **ClickHouse HTTP** | 8123 | http://localhost:8123 |
| **ClickHouse Native** | 9000 | - |
| **Kafka** | 9092 | `localhost:9092` |
| **Redis** | 6379 | `redis-cli` |
| **Grafana** | 3000 | http://localhost:3000 |
| **Prometheus** | 9090 | http://localhost:9090 |

---

## 🐳 Docker 命令

```bash
# 启动所有服务
docker-compose -f infra/compose.yml up -d

# 查看状态
docker-compose -f infra/compose.yml ps

# 查看日志
docker-compose -f infra/compose.yml logs -f [service]

# 停止所有
docker-compose -f infra/compose.yml down

# 重启单个服务
docker-compose -f infra/compose.yml restart clickhouse
```

---

## 🗄️ PostgreSQL 命令

```bash
# 连接数据库
psql -U postgres -d test

# 查看表
psql -U postgres -d test -c "\dt core.*"

# 查询数据
psql -U postgres -d test -c "SELECT COUNT(*) FROM core.core_order;"

# 备份
bash scripts/backup_postgres.sh

# 恢复
bash scripts/restore_postgres.sh

# 测试连接
python scripts/test_postgres_connection.py
```

---

## 📊 ClickHouse 命令

```bash
# 进入客户端
docker exec -it trading_clickhouse clickhouse-client

# 查看表
docker exec trading_clickhouse clickhouse-client --query "SHOW TABLES FROM trading"

# 查询数据
docker exec trading_clickhouse clickhouse-client --query "
  SELECT count() FROM trading.bars_1m_ibkr
"

# 查看大小
docker exec trading_clickhouse clickhouse-client --query "
  SELECT
    name,
    formatReadableSize(total_bytes) as size,
    total_rows
  FROM system.tables
  WHERE database = 'trading'
  ORDER BY total_bytes DESC
"
```

---

## 📨 Kafka 命令

```bash
# 列出 topics
docker exec trading_kafka kafka-topics \
  --list --bootstrap-server localhost:9092

# 创建 topic
docker exec trading_kafka kafka-topics \
  --create --topic test \
  --bootstrap-server localhost:9092

# 查看消息
docker exec trading_kafka kafka-console-consumer \
  --topic md.bar.1m.ibkr \
  --bootstrap-server localhost:9092 \
  --from-beginning --max-messages 5
```

---

## 🔍 状态检查

```bash
# PostgreSQL
pg_isready -h localhost -p 5432

# Docker 服务
docker-compose -f infra/compose.yml ps

# ClickHouse
curl http://localhost:8123

# Redis
redis-cli ping

# Kafka
docker exec trading_kafka kafka-broker-api-versions --bootstrap-server localhost:9092

# 完整测试
python scripts/test_postgres_connection.py
```

---

## 🚨 常见问题 1分钟解决

### PostgreSQL 连不上

```bash
# 1. 检查服务
pg_isready -h localhost -p 5432

# 2. 启动服务
brew services start postgresql@16  # macOS

# 3. 测试
psql -U postgres -d test -c "SELECT 1;"
```

### Docker 服务启动失败

```bash
# 1. 查看日志
docker-compose -f infra/compose.yml logs [service]

# 2. 检查端口
lsof -i :8123  # ClickHouse
lsof -i :9092  # Kafka

# 3. 重启
docker-compose -f infra/compose.yml restart [service]
```

### Python 连接失败

```bash
# 1. 检查 .env
cat .env | grep -E "(PG_|CH_|KAFKA_|REDIS_)"

# 2. 测试连接
python scripts/test_postgres_connection.py

# 3. 确认服务运行
docker-compose -f infra/compose.yml ps
pg_isready
```

---

## 📁 文件位置

### 配置文件

```
trading/
  ├── .env                    # 环境变量（最重要！）
  ├── config.yml              # 应用配置
  ├── infra/compose.yml       # Docker 配置
  └── grafana/
      ├── dashboards/         # Grafana 面板
      └── provisioning/       # 数据源配置
```

### 数据目录

```
PostgreSQL: /opt/homebrew/var/postgresql@16/  # 本地
ClickHouse: Docker Volume ch_data              # Docker
Kafka: Docker Volume kafka_data                # Docker
Redis: Docker Volume redis_data                # Docker
```

---

## 📖 文档快速跳转

```bash
MIGRATION_INDEX.md          # 📋 总索引（从这里开始）
README_MIGRATION.md         # ⭐ 快速开始
SERVICES_SETUP.md           # 🏗️ 架构说明
POSTGRES_MIGRATION.md       # 🐘 PostgreSQL 专项
CONFIG_SYNC.md              # ⚙️ 配置对比
DOCKER_VS_LOCAL.md          # 🤔 技术选型
MIGRATION_GUIDE.md          # 📚 完整指南
QUICK_REFERENCE.md          # ⚡ 本文档
```

---

## ⏱️ 时间表

| 任务 | 时间 |
|------|------|
| 阅读文档 | 15min |
| 备份数据 | 30-60min |
| 传输文件 | 30-120min |
| 新机器恢复 | 20-40min |
| 验证测试 | 10min |
| **总计** | **2-4小时** |

---

## ✅ 成功标志

```bash
# 所有绿色 = 成功！
✓ docker-compose ps 显示所有 "Up"
✓ pg_isready 返回 "accepting connections"
✓ curl localhost:8123 返回 "Ok."
✓ python scripts/test_postgres_connection.py 全部通过
✓ http://localhost:3000 显示 Grafana
```

---

## 🎯 核心策略

```
数据迁移优先级:
  1. ClickHouse（关键数据）
  2. PostgreSQL（策略配置）
  3. 代码和配置
  4. 其他不需要（Docker 重建）

技术选择:
  PostgreSQL: 本地（已有）
  其他: Docker（统一）

配置管理:
  .env 文件（最重要）
  config.yml（应用）
  Docker 默认配置（服务）
```

---

## 💡 最佳实践

```bash
# 1. 备份前检查数据量
/opt/homebrew/bin/clickhouse client --query "
  SELECT formatReadableSize(sum(total_bytes))
  FROM system.tables
  WHERE database = 'trading'
"

# 2. 确保磁盘空间（至少 2x 数据量）
df -h ~/

# 3. 验证备份完整性
ls -lh ~/trading_migration_*/clickhouse/*.native

# 4. 测试恢复（可选，在新机器）
# 先恢复一个小表测试
```

---

## 🔗 一键跳转

**开始迁移**: [README_MIGRATION.md](README_MIGRATION.md)

**PostgreSQL**: [POSTGRES_MIGRATION.md](POSTGRES_MIGRATION.md)

**配置问题**: [CONFIG_SYNC.md](CONFIG_SYNC.md)

**技术选型**: [DOCKER_VS_LOCAL.md](DOCKER_VS_LOCAL.md)

**完整指南**: [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md)

---

打印这张卡片，贴在显示器上！📌
