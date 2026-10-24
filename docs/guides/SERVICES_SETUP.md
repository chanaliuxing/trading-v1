# 服务配置总结

## 快速参考

你的交易系统使用**混合架构**：

| 服务 | 部署方式 | 原因 | 迁移方式 |
|------|----------|------|----------|
| **PostgreSQL** | 🖥️ 本地安装 | 已安装，性能好 | 独立脚本 |
| **其他所有服务** | 🐳 Docker | 统一管理 | Docker Compose |

## 服务架构图

```
┌─────────────────────────────────────┐
│ 应用层（Python）                     │
│ ~/Documents/trading/                 │
│   - main.py                          │
│   - strategies/                      │
│   - venv/                            │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 数据层                               │
│                                      │
│ PostgreSQL (本地安装) 🖥️            │
│   localhost:5432                     │
│   - 订单、策略、HPO试验              │
│   - 32 张表，1.8MB                   │
│                                      │
│ Docker 服务 🐳                      │
│   ├─ ClickHouse (8123, 9000)        │
│   ├─ Kafka (9092)                   │
│   ├─ Redis (6379)                   │
│   ├─ Grafana (3000)                 │
│   ├─ Prometheus (9090)              │
│   ├─ Loki (3100)                    │
│   └─ Tempo (3200)                   │
└─────────────────────────────────────┘
```

## PostgreSQL - 本地安装 🖥️

### 为什么本地？

```yaml
你的情况:
  ✅ 已安装并配置好 PostgreSQL
  ✅ 数据量小（1.8MB）
  ✅ 频繁使用 psql 等工具
  ✅ 性能要求

决策:
  → 保持本地安装
  → 不使用 Docker
  → 独立迁移
```

### 迁移步骤

```bash
# 当前机器
bash scripts/backup_postgres.sh

# 新机器
bash scripts/restore_postgres.sh
```

详细文档: [POSTGRES_MIGRATION.md](POSTGRES_MIGRATION.md)

### 配置

```bash
# .env
PG_HOST=localhost
PG_PORT=5432
PG_DB=test
PG_USER=postgres
PG_PASSWORD=postgres
```

### 测试连接

```bash
# 命令行测试
psql -U postgres -d test -c "SELECT COUNT(*) FROM core.core_order;"

# Python 测试
python scripts/test_postgres_connection.py
```

---

## 其他服务 - Docker 🐳

### 为什么 Docker？

```yaml
优势:
  ✅ 一键启动所有服务
  ✅ 版本管理简单
  ✅ 环境隔离
  ✅ 配置文件化
  ✅ 迁移方便

服务列表:
  - ClickHouse: 交易数据（bars_1m, bars_5m 等）
  - Kafka: 消息队列
  - Redis: 状态缓存
  - Grafana: 可视化
  - Prometheus: 监控
  - Loki + Promtail: 日志
  - Tempo: 分布式追踪
```

### 一键启动

```bash
# 启动所有 Docker 服务
cd ~/Documents/trading
docker-compose -f infra/compose.yml up -d

# 查看状态
docker-compose -f infra/compose.yml ps

# 查看日志
docker-compose -f infra/compose.yml logs -f

# 停止所有
docker-compose -f infra/compose.yml down
```

---

## 完整迁移流程

### 当前机器备份

```bash
cd ~/Documents/trading

# 1. 主备份脚本（包含 ClickHouse 等）
bash scripts/backup_for_migration.sh
# 会提示是否同时备份 PostgreSQL

# 或分开执行:

# 2. ClickHouse + 代码（主脚本）
bash scripts/backup_for_migration.sh

# 3. PostgreSQL（独立脚本）
bash scripts/backup_postgres.sh
```

### 新机器恢复

```bash
# 1. 安装 Docker Desktop
# https://www.docker.com/products/docker-desktop

# 2. 安装 PostgreSQL（本地）
# macOS:
brew install postgresql@16
brew services start postgresql@16

# 3. 恢复主备份（Docker 服务 + ClickHouse）
bash scripts/restore_on_new_machine.sh

# 4. 恢复 PostgreSQL（独立）
cd ~/postgres_backup_YYYYMMDD_HHMMSS
bash restore_postgres.sh
```

---

## 服务访问地址

### PostgreSQL（本地）
```
连接: localhost:5432
命令: psql -U postgres -d test
工具: pgAdmin, DBeaver
```

### Docker 服务
```
ClickHouse HTTP:  http://localhost:8123
ClickHouse Native: localhost:9000
Grafana:          http://localhost:3000 (admin/admin)
Prometheus:       http://localhost:9090
Kafka:            localhost:9092
Redis:            localhost:6379
Loki:             http://localhost:3100
Tempo:            http://localhost:3200
```

---

## 数据重要性分级

### 🔴 必须迁移（关键数据）

1. **ClickHouse** (libs/clickhouse/)
   - 所有历史 bars 数据（bars_1m_ibkr, bars_5m_ibkr 等）
   - 市场数据湖
   - 迁移方法: Native 格式导出/导入
   - 脚本: `backup_for_migration.sh`

2. **PostgreSQL** (localhost:5432)
   - 订单记录（core.core_order）
   - 策略配置（core.core_strategy）
   - HPO 试验数据（core.trials）
   - 风控事件（core.core_risk_event）
   - 迁移方法: pg_dump/pg_restore
   - 脚本: `backup_postgres.sh`

3. **项目代码**
   - 所有 Python 代码
   - 配置文件（config.yml, .env）
   - 策略配置（strategies/configs/）
   - Grafana dashboards
   - 迁移方法: tar.gz 打包

### 🟡 可选迁移

- **Redis** (localhost:6379)
  - 用途: 状态缓存
  - 可重建: 是
  - 迁移: 可选（通常不需要）

### 🟢 不需要迁移

- **Kafka** - 临时消息，重启后重建
- **Prometheus** - 监控数据，可重建
- **Grafana** - Dashboard 在代码中
- **Loki/Promtail** - 日志，不需历史
- **Tempo** - 追踪，不需历史

---

## 启动顺序

### 正确的启动顺序

```bash
# 1. PostgreSQL（本地，独立启动）
brew services start postgresql@16  # macOS
# 或
sudo systemctl start postgresql    # Linux

# 2. Docker 服务（自动处理依赖）
cd ~/Documents/trading
docker-compose -f infra/compose.yml up -d
# Docker Compose 会自动按依赖顺序启动:
#   Zookeeper → Kafka → Schema Registry
#   ClickHouse
#   Redis
#   Prometheus → Grafana
#   Loki, Promtail, Tempo

# 3. Python 应用
source venv/bin/activate
python main.py
```

### 验证启动

```bash
# 检查 PostgreSQL
pg_isready -h localhost -p 5432

# 检查 Docker 服务
docker-compose -f infra/compose.yml ps

# 测试连接
python scripts/test_postgres_connection.py
```

---

## 配置文件说明

### .env (环境变量)

```bash
# PostgreSQL - 本地安装
PG_HOST=localhost
PG_PORT=5432
PG_DB=test
PG_USER=postgres
PG_PASSWORD=postgres

# ClickHouse - Docker
CH_HOST=localhost
CH_PORT=8123
CH_DB=trading
CH_USER=default
CH_PASSWORD=

# Kafka - Docker
KAFKA_BOOTSTRAP_SERVERS=localhost:9092

# Redis - Docker
STATEBUS_REDIS=redis://127.0.0.1:6379/0

# IB 连接
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002
IB_CLIENT_ID=7
```

### infra/compose.yml (Docker 配置)

```yaml
services:
  # PostgreSQL 已注释（使用本地安装）
  # postgres: ...

  clickhouse: ...
  kafka: ...
  redis: ...
  grafana: ...
  # ... 其他服务

volumes:
  # pg_data: ...  # PostgreSQL 不需要
  ch_data:        # ClickHouse 数据持久化
  kafka_data:     # Kafka 数据（可选）
  redis_data:     # Redis 数据（可选）
  # ...
```

---

## 常用命令速查

### PostgreSQL

```bash
# 连接
psql -U postgres -d test

# 查看表
psql -U postgres -d test -c "\dt core.*"

# 查询
psql -U postgres -d test -c "SELECT COUNT(*) FROM core.core_order;"

# 备份
bash scripts/backup_postgres.sh

# 恢复
bash scripts/restore_postgres.sh
```

### Docker 服务

```bash
# 启动所有
docker-compose -f infra/compose.yml up -d

# 停止所有
docker-compose -f infra/compose.yml down

# 查看状态
docker-compose -f infra/compose.yml ps

# 查看日志
docker-compose -f infra/compose.yml logs -f [service]

# 重启单个服务
docker-compose -f infra/compose.yml restart clickhouse

# 进入容器
docker exec -it trading_clickhouse bash
```

### ClickHouse

```bash
# 客户端
docker exec -it trading_clickhouse clickhouse-client

# 查询
docker exec trading_clickhouse clickhouse-client --query "
  SELECT count() FROM trading.bars_1m_ibkr
"

# 查看表
docker exec trading_clickhouse clickhouse-client --query "
  SHOW TABLES FROM trading
"
```

### Kafka

```bash
# 列出 topics
docker exec trading_kafka kafka-topics \
  --list --bootstrap-server localhost:9092

# 查看消息
docker exec trading_kafka kafka-console-consumer \
  --topic md.bar.1m.ibkr \
  --bootstrap-server localhost:9092 \
  --from-beginning --max-messages 5
```

---

## 故障排除

### PostgreSQL 连接失败

```bash
# 1. 检查服务
pg_isready -h localhost -p 5432

# 2. 启动服务
brew services start postgresql@16  # macOS
sudo systemctl start postgresql    # Linux

# 3. 检查配置
cat .env | grep PG_

# 4. 测试连接
psql -U postgres -d test -c "SELECT 1;"
```

### Docker 服务启动失败

```bash
# 1. 查看详细日志
docker-compose -f infra/compose.yml logs [service]

# 2. 检查端口冲突
lsof -i :8123  # ClickHouse
lsof -i :9092  # Kafka
lsof -i :6379  # Redis

# 3. 重新启动
docker-compose -f infra/compose.yml down
docker-compose -f infra/compose.yml up -d

# 4. 清理并重启（危险：会删除数据）
docker-compose -f infra/compose.yml down -v
docker-compose -f infra/compose.yml up -d
```

### Python 连接失败

```bash
# 1. 测试 PostgreSQL
python scripts/test_postgres_connection.py

# 2. 测试 ClickHouse
python -c "from libs.config import config; print(config.clickhouse_config)"

# 3. 检查 .env
cat .env

# 4. 检查 Python 依赖
pip list | grep -E "(psycopg2|clickhouse|kafka)"
```

---

## 监控和管理

### Grafana Dashboards

访问: http://localhost:3000
用户名: admin
密码: admin

已配置的 Dashboard:
- `grafana/dashboards/trading_observability.json`
- `grafana/dashboards/strategy_monitor.json`
- `grafana/dashboards/phase0_infrastructure.json`
- 等等...

### 数据源配置

PostgreSQL:
```yaml
type: postgres
host: host.docker.internal:5432  # Docker 访问主机
database: test
user: postgres
```

ClickHouse:
```yaml
type: clickhouse
host: clickhouse:8123  # Docker 内部网络
database: trading
```

---

## 总结

### 你的混合架构

```
优势:
  ✅ PostgreSQL 本地安装 - 性能最好
  ✅ 其他服务 Docker - 管理方便
  ✅ 明确的数据分离
  ✅ 灵活的迁移策略

迁移时间:
  PostgreSQL: < 5 分钟
  Docker 服务: 30-60 分钟
  总计: < 2 小时
```

### 文档导航

- [README_MIGRATION.md](README_MIGRATION.md) - 总迁移指南
- [POSTGRES_MIGRATION.md](POSTGRES_MIGRATION.md) - PostgreSQL 详细说明
- [DOCKER_VS_LOCAL.md](DOCKER_VS_LOCAL.md) - 技术决策分析
- [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) - 完整迁移步骤

### 脚本清单

```
scripts/
  ├── backup_for_migration.sh      # 主备份（Docker 服务）
  ├── restore_on_new_machine.sh    # 主恢复（Docker 服务）
  ├── backup_postgres.sh           # PostgreSQL 备份
  ├── restore_postgres.sh          # PostgreSQL 恢复
  └── test_postgres_connection.py  # 连接测试
```

---

**混合架构，最佳实践！** 🚀

PostgreSQL 本地安装 + Docker 其他服务 = 完美组合
