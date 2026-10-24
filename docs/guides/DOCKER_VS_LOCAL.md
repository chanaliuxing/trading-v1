# Docker vs 本地安装 - 服务决策指南

## 快速决策表

| 服务 | 推荐方案 | 数据持久化 | 迁移难度 | 理由 |
|------|----------|------------|----------|------|
| **ClickHouse** | 🐳 Docker + Volume | ✅ 必须 | ⭐⭐⭐ 中等 | 数据重要，Docker volume 可靠 |
| **PostgreSQL** | 🐳 Docker + Volume | ✅ 必须 | ⭐⭐ 简单 | pg_dump/restore 成熟 |
| **Kafka** | 🐳 Docker | ❌ 不需要 | ⭐ 极简 | 流式数据，重启即可 |
| **Redis** | 🐳 Docker | ❌ 可选 | ⭐ 极简 | 缓存数据，可重建 |
| **Grafana** | 🐳 Docker | ✅ 配置文件 | ⭐ 极简 | Dashboard 已在代码中 |
| **Prometheus** | 🐳 Docker | ❌ 不需要 | ⭐ 极简 | 监控数据，可重建 |
| **Loki** | 🐳 Docker | ❌ 不需要 | ⭐ 极简 | 日志，不需历史数据 |
| **Tempo** | 🐳 Docker | ❌ 不需要 | ⭐ 极简 | 追踪，不需历史数据 |

图例:
- 🐳 Docker: 推荐使用 Docker
- 💻 本地: 推荐本地安装
- ⭐ 迁移难度 (1-5星)

---

## 详细分析

### 1. ClickHouse - 推荐 Docker ✅

#### 为什么适合 Docker？
```yaml
优势:
  ✅ 版本管理简单 (image: clickhouse/clickhouse-server:latest)
  ✅ 配置隔离（不污染系统）
  ✅ 数据持久化可靠（Docker Named Volumes）
  ✅ 备份恢复方便
  ✅ 跨平台一致性（Mac/Linux/Windows）

风险:
  ⚠️ 数据必须正确配置持久化卷

结论: 强烈推荐 Docker
```

#### 数据持久化配置
```yaml
# infra/compose.yml 已配置
clickhouse:
  volumes:
    - ch_data:/var/lib/clickhouse  # ✅ 数据安全
    - ./init/sql/ch_init.sql:/docker-entrypoint-initdb.d/init.sql

volumes:
  ch_data:  # Docker 管理的命名卷，即使容器删除数据也保留
```

#### 迁移方案
```bash
# 当前机器导出
/opt/homebrew/bin/clickhouse client --query "
  SELECT * FROM trading.bars_1m_ibkr FORMAT Native
" > bars_1m.native

# 新机器导入（Docker）
docker exec -i trading_clickhouse clickhouse-client --query "
  INSERT INTO trading.bars_1m_ibkr FORMAT Native
" < bars_1m.native
```

---

### 2. Kafka - 推荐 Docker ✅

#### 为什么适合 Docker？
```yaml
优势:
  ✅ 配置复杂（Zookeeper依赖），Docker一键启动
  ✅ 多组件协调（Kafka + Zookeeper + Schema Registry）
  ✅ 版本兼容性管理简单
  ✅ 不需要迁移历史数据

风险:
  ❌ 几乎没有

结论: 完美适合 Docker
```

#### 数据策略
```yaml
你的配置:
  retention: 168小时（7天）
  用途: 实时事件流

建议:
  ❌ 不迁移 Kafka 数据
  ✅ 新机器直接启动
  ✅ 应用重启后会产生新消息
```

#### Docker 配置
```yaml
# infra/compose.yml 已包含完整配置
services:
  zookeeper:
    image: confluentinc/cp-zookeeper:7.5.0
    # ... 配置完善

  kafka:
    image: confluentinc/cp-kafka:7.5.0
    depends_on:
      zookeeper:
        condition: service_healthy
    # ... 自动等待依赖启动

  schema-registry:
    image: confluentinc/cp-schema-registry:7.5.0
    # ... 完整的 Kafka 生态
```

---

### 3. Redis - 推荐 Docker ✅

#### 为什么适合 Docker？
```yaml
优势:
  ✅ 轻量级服务
  ✅ 缓存数据可重建
  ✅ 配置简单

用途分析:
  你的使用: 状态缓存（statebus）
  数据特征: 临时性，应用启动时重建

结论: Docker 完全足够
```

#### 数据迁移决策
```python
# 你的代码中 Redis 用途
STATEBUS_REDIS = 'redis://127.0.0.1:6379/0'

# 数据类型: 运行时状态
# 持久化需求: 低
# 迁移建议: 不需要
```

---

### 4. PostgreSQL - 推荐 Docker ✅

#### 为什么适合 Docker？
```yaml
优势:
  ✅ 成熟的备份恢复工具（pg_dump）
  ✅ 数据持久化可靠
  ✅ 版本管理方便

迁移:
  ⭐⭐ 非常简单
  工具: pg_dump / pg_restore
```

#### 迁移示例
```bash
# 导出（当前机器）
pg_dump -U postgres test > backup.sql

# 导入（新机器 Docker）
docker exec -i trading_postgres psql -U postgres test < backup.sql

# 完成！
```

---

### 5. 可观测性栈 - 推荐 Docker ✅

#### Grafana
```yaml
为什么 Docker？
  ✅ Dashboard 已在代码中（grafana/dashboards/*.json）
  ✅ 数据源配置文件化（grafana/provisioning/datasources/ds.yml）
  ✅ 不需要迁移数据，只需配置

迁移:
  复制代码 → Docker启动 → Dashboard 自动加载
```

#### Prometheus
```yaml
为什么 Docker？
  ✅ 监控数据不需要历史记录
  ✅ 配置文件管理（infra/prometheus.yml）
  ✅ 新环境重新采集即可

迁移:
  直接启动，无需迁移数据
```

#### Loki + Promtail
```yaml
为什么 Docker？
  ✅ 日志聚合，不需要历史日志
  ✅ 配置文件管理
  ✅ 轻量级服务

迁移:
  复制配置文件 → Docker启动
```

#### Tempo
```yaml
为什么 Docker？
  ✅ 分布式追踪，不需要历史数据
  ✅ 配置文件管理

迁移:
  直接启动
```

---

## Docker 数据持久化详解

### Named Volumes vs Bind Mounts

#### Named Volumes（推荐用于数据库）
```yaml
# infra/compose.yml
services:
  clickhouse:
    volumes:
      - ch_data:/var/lib/clickhouse  # ← Named Volume

volumes:
  ch_data:  # Docker 管理，数据安全

优势:
  ✅ Docker 自动管理
  ✅ 跨平台兼容
  ✅ 备份方便（docker run --volumes-from）
  ✅ 不会被误删
```

#### Bind Mounts（推荐用于配置文件）
```yaml
services:
  grafana:
    volumes:
      - ./grafana/dashboards:/var/lib/grafana/dashboards  # ← Bind Mount

优势:
  ✅ 直接编辑本地文件
  ✅ Git 版本控制
  ✅ 实时同步
```

### 数据位置对比

| 服务 | 本地安装数据位置 | Docker Volume | 迁移方法 |
|------|------------------|---------------|----------|
| ClickHouse | `/opt/homebrew/var/lib/clickhouse` | `ch_data` volume | Native 格式导出/导入 |
| PostgreSQL | `/opt/homebrew/var/postgres` | `pg_data` volume | pg_dump/restore |
| Kafka | `/opt/homebrew/var/lib/kafka` | `kafka_data` volume | 不需要迁移 |
| Redis | `/opt/homebrew/var/db/redis` | `redis_data` volume | 可选（SAVE + copy） |

---

## 为什么你的情况特别适合 Docker？

### 1. 你已有完整的 Docker Compose 配置
```bash
# 你的 infra/compose.yml 已包含:
✅ PostgreSQL
✅ ClickHouse
✅ Redis
✅ Kafka + Zookeeper
✅ Schema Registry
✅ Prometheus
✅ Grafana

# 只需一条命令:
docker-compose -f infra/compose.yml up -d
```

### 2. 迁移场景完美契合
```yaml
迁移痛点:
  ❌ 手动安装每个服务（Homebrew, apt-get等）
  ❌ 版本不一致
  ❌ 依赖冲突
  ❌ 配置路径不同

Docker 解决方案:
  ✅ 一次配置，到处运行
  ✅ 版本锁定（image: clickhouse:latest）
  ✅ 环境隔离
  ✅ 统一管理（docker-compose）
```

### 3. 你的数据特征
```yaml
重要数据:
  - ClickHouse 交易数据 → Docker Volume 安全
  - PostgreSQL 元数据 → pg_dump 成熟

临时数据:
  - Kafka 消息 → 不需要迁移
  - Redis 缓存 → 可重建
  - 日志/追踪 → 不需要历史
```

---

## 实战：对比两种方案

### 方案A: 本地安装（当前方式）

```bash
# 新机器需要:
brew install clickhouse
brew install kafka
brew install redis
brew install postgresql
brew install grafana
brew install loki
brew install promtail
brew install tempo
brew install prometheus

# 配置每个服务
vim /opt/homebrew/etc/clickhouse-server/config.xml
vim /opt/homebrew/etc/kafka/server.properties
vim /opt/homebrew/etc/grafana/grafana.ini
# ... 更多配置

# 启动每个服务
brew services start clickhouse
brew services start kafka
brew services start redis
# ... 更多启动命令

# 迁移数据
# ... 每个服务不同的迁移方法

总耗时: 4-6 小时
复杂度: ⭐⭐⭐⭐⭐
```

### 方案B: Docker Compose（推荐）

```bash
# 新机器需要:
# 1. 安装 Docker Desktop (10分钟)

# 2. 恢复代码
tar -xzf trading_code.tar.gz
cd trading

# 3. 启动所有服务
docker-compose -f infra/compose.yml up -d

# 4. 恢复 ClickHouse 数据
./scripts/restore_clickhouse.sh

# 5. 完成！

总耗时: 1-2 小时
复杂度: ⭐⭐
```

---

## Docker 常见误解

### 误解1: "Docker 不适合数据库"
```yaml
事实:
  ✅ Docker Named Volumes 与本地存储性能相当
  ✅ 生产环境广泛使用（Kubernetes StatefulSet）
  ✅ 备份恢复更方便

证据:
  - PostgreSQL 官方 Docker 镜像
  - ClickHouse 官方支持 Docker
  - 你的 infra/compose.yml 已配置 volumes
```

### 误解2: "Docker 容器删除会丢数据"
```yaml
事实:
  ✅ Named Volumes 独立于容器
  ❌ 只有容器被删除
  ✅ 数据保留在 Volume 中

验证:
  docker-compose down        # 删除容器
  docker volume ls           # Volume 还在
  docker-compose up -d       # 重启，数据完整
```

### 误解3: "Docker 性能差"
```yaml
事实:
  ✅ 原生 Linux: 几乎零开销
  ✅ macOS: 使用虚拟化，99% 性能
  ⚠️ Windows: WSL2 性能优秀

你的场景:
  交易系统 → ClickHouse 查询延迟
  本地: ~10ms
  Docker: ~11ms (可忽略差异)
```

---

## 推荐配置总结

### 新机器完整架构

```
┌─────────────────────────────────────┐
│ 应用层（本地 Python）                │
│ ~/Documents/trading/                 │
│   venv/                              │
│   main.py                            │
│   strategies/                        │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 基础设施层（Docker）                 │
│ docker-compose -f infra/compose.yml  │
│                                      │
│  持久化服务（Named Volumes）:        │
│  ├─ ClickHouse (ch_data)            │
│  ├─ PostgreSQL (pg_data)            │
│  └─ Kafka (kafka_data) *可选         │
│                                      │
│  无状态服务:                         │
│  ├─ Redis                            │
│  ├─ Grafana                          │
│  ├─ Prometheus                       │
│  ├─ Loki + Promtail                  │
│  └─ Tempo                            │
└─────────────────────────────────────┘
```

### 一键启动命令

```bash
# 完整启动
cd ~/Documents/trading
docker-compose -f infra/compose.yml up -d

# 查看状态
docker-compose -f infra/compose.yml ps

# 查看日志
docker-compose -f infra/compose.yml logs -f

# 停止所有
docker-compose -f infra/compose.yml down

# 停止并删除数据（危险！）
docker-compose -f infra/compose.yml down -v
```

---

## 最终建议

### 对于你的迁移场景

```yaml
强烈推荐:
  ✅ 所有服务使用 Docker
  ✅ 使用现有的 infra/compose.yml
  ✅ 只迁移 ClickHouse 数据（最重要）
  ✅ 其他服务新环境重建

理由:
  1. 你已有完整的 Docker 配置
  2. 迁移更快更可靠
  3. 未来再次迁移更简单
  4. 环境一致性好
  5. 问题隔离和调试方便
```

### 迁移步骤优先级

```
第一优先级（必须）:
  1. ✅ ClickHouse 数据导出
  2. ✅ 项目代码打包
  3. ✅ .env 配置文件

第二优先级（推荐）:
  4. ⚪ PostgreSQL 备份（如果使用）

第三优先级（可选）:
  5. ⚪ Redis 备份
  6. ⚪ Grafana 自定义配置

不需要:
  ❌ Kafka 数据
  ❌ 日志文件
  ❌ Prometheus 数据
```

---

## 快速决策流程图

```
需要迁移服务？
    ↓
是数据库吗？
    ↓ 是
有历史数据吗？
    ↓ 是
→ 使用 Docker + Volume
→ 导出数据 → 新机器导入

    ↓ 否（临时数据）
→ 使用 Docker
→ 不需要迁移数据

    ↓ 否（非数据库）
是配置文件管理吗？
    ↓ 是
→ 使用 Docker + Bind Mount
→ 复制配置文件

    ↓ 否
→ 使用 Docker
→ 新环境重建
```

---

## 总结

**你的项目完美适合完全 Docker 化！**

优势:
- ✅ 配置文件已完整（infra/compose.yml）
- ✅ 数据特征适合容器化
- ✅ 迁移场景完美契合
- ✅ 未来维护更简单

行动:
1. 运行 `scripts/backup_for_migration.sh` 备份数据
2. 复制到新机器
3. 运行 `scripts/restore_on_new_machine.sh` 恢复
4. 完成！

时间: 2-3 小时（vs 手动 6+ 小时）
