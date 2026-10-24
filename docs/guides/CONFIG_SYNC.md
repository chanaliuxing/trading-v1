# 配置文件同步指南

## 配置文件位置对比

### 当前环境（Homebrew 本地安装）

| 服务 | 配置文件位置 | 数据目录 |
|------|-------------|----------|
| **PostgreSQL** | `/opt/homebrew/var/postgresql@16/` | `/opt/homebrew/var/postgresql@16/` |
| **ClickHouse** | `/opt/homebrew/etc/clickhouse-server/config.xml` | `/opt/homebrew/var/lib/clickhouse/` |
| **Kafka** | `/opt/homebrew/etc/kafka/server.properties` | `/opt/homebrew/var/lib/kafka/` |
| **Redis** | `/opt/homebrew/etc/redis.conf` | `/opt/homebrew/var/db/redis/` |
| **Grafana** | `/opt/homebrew/etc/grafana/grafana.ini` | `/opt/homebrew/var/lib/grafana/` |

### 新环境（Docker）

| 服务 | 配置文件位置 | 数据目录 |
|------|-------------|----------|
| **PostgreSQL** | 本地安装（同上） | 本地安装（同上） |
| **ClickHouse** | 容器内 `/etc/clickhouse-server/` | Docker Volume `ch_data` |
| **Kafka** | 容器内 `/etc/kafka/` | Docker Volume `kafka_data` |
| **Redis** | 容器内 `/etc/redis/` | Docker Volume `redis_data` |
| **Grafana** | 容器内 `/etc/grafana/` | Docker Volume `grafana_data` |

---

## 关键配置对比

### 1. PostgreSQL（本地安装，无需改变）

#### 当前配置
```bash
# 数据目录
/opt/homebrew/var/postgresql@16/

# 配置文件
/opt/homebrew/var/postgresql@16/postgresql.conf
/opt/homebrew/var/postgresql@16/pg_hba.conf

# 端口
5432

# 用户
postgres
```

#### 新机器配置
**保持一致**，也使用本地安装，配置完全相同。

---

### 2. ClickHouse

#### 当前配置（Homebrew）
```xml
<!-- /opt/homebrew/etc/clickhouse-server/config.xml -->
<yandex>
    <logger>
        <level>information</level>
        <log>/opt/homebrew/var/log/clickhouse-server/clickhouse-server.log</log>
    </logger>
    <http_port>8123</http_port>
    <tcp_port>9000</tcp_port>
    <path>/opt/homebrew/var/lib/clickhouse/</path>
</yandex>
```

#### Docker 配置
```yaml
# infra/compose.yml
clickhouse:
  image: clickhouse/clickhouse-server:latest
  ports:
    - "8123:8123"  # HTTP ✓ 一致
    - "9000:9000"  # Native ✓ 一致
  volumes:
    - ch_data:/var/lib/clickhouse  # 数据持久化
  environment:
    CLICKHOUSE_DB: trading  # ✓ 一致
    CLICKHOUSE_USER: default  # ✓ 一致
    CLICKHOUSE_PASSWORD: ""  # ✓ 一致
```

**结论**: 端口和数据库名称一致，应用代码无需修改。

---

### 3. Kafka

#### 当前配置（Homebrew）
```properties
# /opt/homebrew/etc/kafka/server.properties
broker.id=1
listeners=PLAINTEXT://localhost:9092
log.dirs=/opt/homebrew/var/lib/kafka
zookeeper.connect=localhost:2181
log.retention.hours=168
```

#### Docker 配置
```yaml
# infra/compose.yml
kafka:
  image: confluentinc/cp-kafka:7.5.0
  ports:
    - "9092:9092"  # ✓ 一致
  environment:
    KAFKA_BROKER_ID: 1  # ✓ 一致
    KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://localhost:9092  # ✓ 一致
    KAFKA_LOG_RETENTION_HOURS: 168  # ✓ 一致
```

**结论**: 配置一致，应用代码无需修改。

---

### 4. Redis

#### 当前配置（Homebrew）
```conf
# /opt/homebrew/etc/redis.conf
port 6379
dir /opt/homebrew/var/db/redis
dbfilename dump.rdb
maxmemory 0  # 无限制
```

#### Docker 配置
```yaml
# infra/compose.yml
redis:
  image: redis:7-alpine
  ports:
    - "6379:6379"  # ✓ 一致
  volumes:
    - redis_data:/data
```

**结论**: 配置一致（使用默认配置），应用代码无需修改。

---

### 5. Grafana

#### 当前配置（Homebrew）
```ini
# /opt/homebrew/etc/grafana/grafana.ini
[server]
http_port = 3000

[paths]
data = /opt/homebrew/var/lib/grafana
logs = /opt/homebrew/var/log/grafana
plugins = /opt/homebrew/var/lib/grafana/plugins

[database]
type = sqlite3
path = grafana.db
```

#### Docker 配置
```yaml
# infra/compose.yml
grafana:
  image: grafana/grafana:latest
  ports:
    - "3000:3000"  # ✓ 一致
  environment:
    GF_SECURITY_ADMIN_USER: admin
    GF_SECURITY_ADMIN_PASSWORD: admin
  volumes:
    - grafana_data:/var/lib/grafana
    - ../dashboards:/etc/grafana/provisioning/dashboards  # ✓ 自动加载
```

**重要**: Dashboards 通过 volume 挂载自动加载！

---

## 应用配置文件分析

### config.yml（应用主配置）

你的应用不直接读取服务配置，而是通过 **config.yml + .env** 连接服务：

```yaml
# config.yml（部分）
clickhouse:
  timezone: America/New_York

kafka:
  bootstrap_servers: "127.0.0.1:9092"  # 从 .env 覆盖
  acks: "all"
  compression_type: "lz4"

# 这些配置在 .env 中覆盖
# CH_HOST, CH_PORT, KAFKA_BOOTSTRAP_SERVERS 等
```

### .env（环境变量，关键！）

```bash
# PostgreSQL - 本地安装
PG_HOST=localhost
PG_PORT=5432
PG_DB=test
PG_USER=postgres
PG_PASSWORD=postgres

# ClickHouse - Docker（连接到 localhost）
CH_HOST=localhost
CH_PORT=8123
CH_DB=trading
CH_USER=default
CH_PASSWORD=

# Kafka - Docker（连接到 localhost）
KAFKA_BOOTSTRAP_SERVERS=localhost:9092

# Redis - Docker（连接到 localhost）
STATEBUS_REDIS=redis://127.0.0.1:6379/0

# IB 连接
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002
IB_CLIENT_ID=7
ENV_MODE=paper
```

**关键点**:
- 所有服务都监听 `localhost`
- Docker 服务通过端口映射暴露到主机
- 应用连接 `localhost:端口`，无论服务在 Docker 还是本地

---

## 配置一致性检查清单

### ✅ 已确认一致

| 项目 | 当前 | Docker | 状态 |
|------|------|--------|------|
| PostgreSQL 端口 | 5432 | N/A（本地） | ✅ 一致 |
| ClickHouse HTTP | 8123 | 8123 | ✅ 一致 |
| ClickHouse Native | 9000 | 9000 | ✅ 一致 |
| Kafka Bootstrap | 9092 | 9092 | ✅ 一致 |
| Redis 端口 | 6379 | 6379 | ✅ 一致 |
| Grafana 端口 | 3000 | 3000 | ✅ 一致 |
| Kafka 保留时间 | 168h | 168h | ✅ 一致 |

### ⚠️ 需要注意的差异

| 项目 | 当前 | Docker | 影响 |
|------|------|--------|------|
| 数据目录 | Homebrew 路径 | Docker Volume | ✅ 无影响（应用不直接访问） |
| 配置文件 | Homebrew 路径 | 容器内 | ✅ 无影响（通过环境变量） |
| 日志位置 | `/opt/homebrew/var/log/` | 容器内 | ⚠️ 需要用 `docker logs` |

---

## 迁移时配置处理

### 需要迁移的配置

#### 1. Grafana Dashboards（已在代码中）✅
```bash
grafana/dashboards/
  ├── trading_observability.json
  ├── strategy_monitor.json
  ├── phase0_infrastructure.json
  └── ...

# Docker 自动加载！
volumes:
  - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
```

#### 2. Grafana 数据源配置（已在代码中）✅
```yaml
# grafana/provisioning/datasources/ds.yml
datasources:
  - name: ClickHouse
    type: clickhouse
    url: http://clickhouse:8123  # Docker 内部网络
    database: trading

  - name: PostgreSQL
    type: postgres
    url: host.docker.internal:5432  # Docker 访问主机
    database: test
```

**关键**: PostgreSQL 使用 `host.docker.internal` 从 Docker 访问主机！

#### 3. 应用配置（需要复制）✅
```bash
# 备份脚本会自动处理
backup_for_migration.sh:
  - config.yml       ✓
  - .env             ✓
  - strategies/configs/  ✓
  - grafana/dashboards/  ✓
```

### 不需要迁移的配置

#### 服务内部配置（Docker 镜像自带）
- ClickHouse `config.xml`（使用默认）
- Kafka `server.properties`（使用默认）
- Redis `redis.conf`（使用默认）
- Grafana `grafana.ini`（使用默认）

**原因**: Docker 镜像已优化默认配置，你的应用通过环境变量覆盖即可。

---

## 新机器配置验证

### 1. 检查端口一致性

```bash
# 新机器运行这些命令
docker-compose -f infra/compose.yml ps

# 应该看到:
# trading_clickhouse  Up  0.0.0.0:8123->8123/tcp, 0.0.0.0:9000->9000/tcp
# trading_kafka       Up  0.0.0.0:9092->9092/tcp
# trading_redis       Up  0.0.0.0:6379->6379/tcp
# trading_grafana     Up  0.0.0.0:3000->3000/tcp

# PostgreSQL (本地)
pg_isready -h localhost -p 5432
```

### 2. 检查连接

```bash
# ClickHouse
curl http://localhost:8123
# 应该返回: Ok.

# Kafka
docker exec trading_kafka kafka-broker-api-versions --bootstrap-server localhost:9092
# 应该列出 API 版本

# Redis
redis-cli ping
# 应该返回: PONG

# PostgreSQL
psql -U postgres -d test -c "SELECT 1;"
# 应该返回: 1
```

### 3. 检查应用配置

```python
# test_all_connections.py
from libs.config import config

print("ClickHouse:", config.clickhouse_config)
# {'host': 'localhost', 'port': 8123, ...}

print("Kafka:", config.kafka_config)
# {'bootstrap_servers': ['localhost:9092'], ...}

print("Redis:", config.redis_config)
# redis://127.0.0.1:6379/0

print("PostgreSQL:", config.pg_config)
# {'host': 'localhost', 'port': 5432, ...}
```

---

## 常见配置问题

### 问题1: Grafana 看不到 PostgreSQL 数据

**原因**: Docker 容器访问主机数据库

**解决**:
```yaml
# grafana/provisioning/datasources/ds.yml
- name: PostgreSQL
  type: postgres
  url: host.docker.internal:5432  # ← 关键！不是 localhost
  database: test
  user: postgres
```

### 问题2: Python 应用连不上 ClickHouse

**检查**:
```bash
# 1. 确认 Docker 容器运行
docker ps | grep clickhouse

# 2. 确认端口映射
docker port trading_clickhouse
# 应该显示: 8123/tcp -> 0.0.0.0:8123

# 3. 确认 .env 配置
cat .env | grep CH_HOST
# 应该是: CH_HOST=localhost

# 4. 测试连接
curl http://localhost:8123
```

### 问题3: Kafka 连不上

**检查**:
```bash
# 1. 确认 Zookeeper 先启动
docker logs trading_zookeeper | tail

# 2. 确认 Kafka 启动完成
docker logs trading_kafka | grep "started (kafka.server.KafkaServer)"

# 3. 测试连接
docker exec trading_kafka kafka-broker-api-versions --bootstrap-server localhost:9092
```

---

## 配置文件备份

### 自动备份（推荐）

备份脚本已包含所有配置：

```bash
bash scripts/backup_for_migration.sh

# 会备份:
├── config.yml             # 应用主配置
├── .env                   # 环境变量（关键！）
├── infra/compose.yml      # Docker 配置
├── grafana/dashboards/    # Grafana 面板
├── grafana/provisioning/  # 数据源配置
└── strategies/configs/    # 策略配置
```

### 手动备份

```bash
# 如果需要手动备份服务配置（通常不需要）
mkdir -p ~/config_backup

# ClickHouse (本地)
cp /opt/homebrew/etc/clickhouse-server/config.xml ~/config_backup/

# Kafka (本地)
cp /opt/homebrew/etc/kafka/server.properties ~/config_backup/

# Redis (本地)
cp /opt/homebrew/etc/redis.conf ~/config_backup/

# Grafana (本地)
cp /opt/homebrew/etc/grafana/grafana.ini ~/config_backup/

# PostgreSQL (本地)
cp /opt/homebrew/var/postgresql@16/postgresql.conf ~/config_backup/
```

---

## 配置文件管理最佳实践

### 1. 版本控制

```bash
# 应该在 Git 中管理的配置
.
├── config.yml                  ✅ 在 Git 中
├── .env.example                ✅ 在 Git 中（示例）
├── .env                        ❌ 不在 Git 中（本地，包含密码）
├── infra/compose.yml           ✅ 在 Git 中
├── grafana/dashboards/         ✅ 在 Git 中
├── grafana/provisioning/       ✅ 在 Git 中
└── strategies/configs/         ✅ 在 Git 中
```

### 2. 环境变量管理

```bash
# .env.example（提交到 Git）
PG_HOST=localhost
PG_PORT=5432
PG_DB=test
PG_USER=your_user
PG_PASSWORD=your_password

# .env（本地，不提交）
PG_HOST=localhost
PG_PORT=5432
PG_DB=test
PG_USER=postgres
PG_PASSWORD=actual_password
```

### 3. 配置优先级

```
应用读取配置的优先级:
1. .env 环境变量（最高优先级）
2. config.yml 应用配置
3. 服务默认配置（最低优先级）

例如:
  config.yml: kafka.bootstrap_servers = "127.0.0.1:9092"
  .env: KAFKA_BOOTSTRAP_SERVERS=my-kafka:9092
  → 使用 .env 的值
```

---

## 总结

### 配置一致性结论

✅ **应用层配置完全一致**
- 所有服务端口相同
- 连接配置相同（通过 .env）
- 数据库名称相同

✅ **服务内部配置不影响应用**
- Docker 使用优化的默认配置
- 本地 Homebrew 配置也适用
- 应用通过环境变量覆盖

✅ **配置迁移自动化**
- 备份脚本包含所有配置
- Grafana Dashboard 自动加载
- 无需手动调整

### 迁移后验证清单

- [ ] 所有服务端口正确（8123, 9092, 6379, 3000, 5432）
- [ ] `.env` 文件已复制
- [ ] `config.yml` 已复制
- [ ] Python 连接测试通过
- [ ] Grafana Dashboards 显示正常
- [ ] Kafka topics 可访问
- [ ] ClickHouse 查询正常
- [ ] PostgreSQL 查询正常

---

**配置一致性：完全保证！** ✅

你的配置已经非常好了，迁移不需要调整任何配置。
