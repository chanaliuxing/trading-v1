# 迁移文档索引

## 📋 快速导航

你正在计划将交易系统迁移到新电脑。这里是完整的文档指南：

```
你的需求: 迁移交易系统到新电脑
推荐方案: PostgreSQL 本地 + 其他服务 Docker
预计时间: 2-4 小时
难度等级: ⭐⭐ 简单（自动化脚本）
```

---

## 🚀 快速开始（推荐路径）

### 第一次了解？从这里开始

1. **[README_MIGRATION.md](README_MIGRATION.md)** ⭐ **推荐首先阅读**
   - 5分钟快速开始
   - 完整迁移流程
   - 常用命令速查

2. **[SERVICES_SETUP.md](SERVICES_SETUP.md)** ⭐ **理解架构**
   - 服务架构图
   - PostgreSQL 为什么用本地
   - 其他服务为什么用 Docker

### 执行迁移？使用这些

3. **当前机器**
   ```bash
   # 一键备份
   bash scripts/backup_for_migration.sh
   ```

4. **新机器**
   ```bash
   # 一键恢复
   bash scripts/restore_on_new_machine.sh
   ```

---

## 📚 详细文档

### 迁移相关

| 文档 | 内容 | 何时查看 |
|------|------|----------|
| [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) | 完整迁移步骤和说明 | 需要详细步骤时 |
| [POSTGRES_MIGRATION.md](POSTGRES_MIGRATION.md) | PostgreSQL 独立迁移 | PostgreSQL 相关问题 |
| [DOCKER_VS_LOCAL.md](DOCKER_VS_LOCAL.md) | Docker vs 本地对比分析 | 想理解技术决策 |

### 配置相关

| 文档 | 内容 | 何时查看 |
|------|------|----------|
| [CONFIG_SYNC.md](CONFIG_SYNC.md) | 配置文件同步和对比 | 担心配置一致性 |
| [SERVICES_SETUP.md](SERVICES_SETUP.md) | 服务配置总结 | 查看服务架构 |

---

## 🛠️ 脚本工具

### 备份脚本（当前机器运行）

| 脚本 | 用途 | 执行时间 |
|------|------|----------|
| `scripts/backup_for_migration.sh` | 主备份（ClickHouse、代码、配置） | 30-60分钟 |
| `scripts/backup_postgres.sh` | PostgreSQL 独立备份 | < 5分钟 |

### 恢复脚本（新机器运行）

| 脚本 | 用途 | 执行时间 |
|------|------|----------|
| `scripts/restore_on_new_machine.sh` | 主恢复（Docker 服务、ClickHouse） | 20-40分钟 |
| `scripts/restore_postgres.sh` | PostgreSQL 独立恢复 | < 5分钟 |

### 测试脚本

| 脚本 | 用途 |
|------|------|
| `scripts/test_postgres_connection.py` | PostgreSQL 连接和数据验证 |

---

## 🎯 按场景查找

### 场景1: 我想快速了解迁移方案

➡️ 阅读: [README_MIGRATION.md](README_MIGRATION.md)
- 5分钟了解全部
- 知道需要做什么

### 场景2: 我想理解为什么这样设计

➡️ 阅读: [SERVICES_SETUP.md](SERVICES_SETUP.md) + [DOCKER_VS_LOCAL.md](DOCKER_VS_LOCAL.md)
- 理解混合架构
- 了解技术决策

### 场景3: 我准备开始迁移了

➡️ 执行:
1. 阅读 [README_MIGRATION.md](README_MIGRATION.md)
2. 运行 `bash scripts/backup_for_migration.sh`
3. 传输备份到新机器
4. 在新机器运行 `bash scripts/restore_on_new_machine.sh`

### 场景4: PostgreSQL 相关问题

➡️ 查看: [POSTGRES_MIGRATION.md](POSTGRES_MIGRATION.md)
- PostgreSQL 详细迁移
- 本地安装配置
- 故障排除

### 场景5: 担心配置不一致

➡️ 查看: [CONFIG_SYNC.md](CONFIG_SYNC.md)
- 配置文件对比
- 一致性检查
- 已验证端口相同

### 场景6: 迁移遇到问题

➡️ 查看故障排除:
- [README_MIGRATION.md](README_MIGRATION.md) - 故障排除部分
- [POSTGRES_MIGRATION.md](POSTGRES_MIGRATION.md) - PostgreSQL 故障排除
- [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) - 常见问题

---

## 📊 你的系统概况

### 数据规模

```
ClickHouse: 需要迁移
  - bars_1m_ibkr, bars_5m_ibkr 等
  - 交易历史数据
  - 大小: 取决于你的数据量

PostgreSQL: 需要迁移 ✅
  - 32 张表
  - 大小: 10 MB
  - 主要内容:
    * 订单记录
    * 策略配置（11条）
    * HPO 试验（72条）
    * 风控事件（25条）

Kafka: 不需要迁移 ✅
  - 临时消息
  - 保留期: 7天
  - 新环境重建

Redis: 不需要迁移 ✅
  - 缓存数据
  - 应用启动时重建

Grafana: 不需要迁移 ✅
  - Dashboards 在代码中
  - 自动加载
```

### 服务架构

```
PostgreSQL (本地)
  ↓
Python 应用 (venv)
  ↓
Docker 服务
  ├─ ClickHouse
  ├─ Kafka
  ├─ Redis
  ├─ Grafana
  └─ Prometheus, Loki, Tempo
```

---

## ✅ 迁移检查清单

### 准备阶段

- [ ] 阅读 [README_MIGRATION.md](README_MIGRATION.md)
- [ ] 确认磁盘空间足够（至少 2x 数据量）
- [ ] 准备传输方式（硬盘/网络/云存储）
- [ ] 了解新机器操作系统

### 当前机器

- [ ] 运行 `bash scripts/backup_for_migration.sh`
- [ ] 运行 `bash scripts/backup_postgres.sh`
- [ ] 验证备份完整性
- [ ] 记录数据量和表数量
- [ ] 传输备份到新机器

### 新机器

- [ ] 安装 Docker Desktop
- [ ] 安装 PostgreSQL（本地）
- [ ] 解压备份文件
- [ ] 运行 `bash scripts/restore_on_new_machine.sh`
- [ ] 运行 PostgreSQL 恢复（如果需要）
- [ ] 验证所有服务启动
- [ ] 验证数据完整性
- [ ] 测试 Python 应用连接

### 验证阶段

- [ ] 所有 Docker 容器运行: `docker-compose ps`
- [ ] PostgreSQL 连接: `python scripts/test_postgres_connection.py`
- [ ] ClickHouse 数据: `docker exec trading_clickhouse clickhouse-client`
- [ ] Kafka 可用: `docker exec trading_kafka kafka-topics --list`
- [ ] Grafana 访问: http://localhost:3000
- [ ] 运行一次策略测试

---

## 🔑 关键概念

### 混合架构

```yaml
决策:
  PostgreSQL: 本地安装
    原因: 已安装、数据小、工具方便

  其他服务: Docker
    原因: 统一管理、易于迁移、配置简单
```

### 数据持久化

```yaml
Docker Named Volumes:
  ch_data: ClickHouse 数据（安全持久化）
  kafka_data: Kafka 数据（可选持久化）
  redis_data: Redis 数据（可选持久化）
  grafana_data: Grafana 数据（Dashboard 在代码中）

本地目录:
  PostgreSQL: /opt/homebrew/var/postgresql@16/
  （新机器位置可能不同，但功能相同）
```

### 配置管理

```yaml
层级:
  1. .env - 环境变量（最高优先级）
  2. config.yml - 应用配置
  3. Docker/服务默认配置

一致性:
  ✅ 所有服务端口相同
  ✅ 通过 .env 统一管理
  ✅ 无需手动调整
```

---

## 📞 获取帮助

### 按优先级查看文档

1. **快速问题** → [README_MIGRATION.md](README_MIGRATION.md) 故障排除
2. **PostgreSQL** → [POSTGRES_MIGRATION.md](POSTGRES_MIGRATION.md)
3. **配置问题** → [CONFIG_SYNC.md](CONFIG_SYNC.md)
4. **技术细节** → [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md)

### 常见问题快速链接

- [如何备份？](README_MIGRATION.md#当前机器备份)
- [如何恢复？](README_MIGRATION.md#新机器恢复)
- [PostgreSQL 怎么迁移？](POSTGRES_MIGRATION.md#快速迁移)
- [配置文件一致吗？](CONFIG_SYNC.md#配置一致性检查清单)
- [为什么用 Docker？](DOCKER_VS_LOCAL.md#docker-vs-本地安装对比)
- [服务访问地址？](SERVICES_SETUP.md#服务访问地址)

---

## 📈 时间估算

| 阶段 | 时间 | 说明 |
|------|------|------|
| **阅读文档** | 15-30分钟 | 了解方案和步骤 |
| **当前机器备份** | 30-60分钟 | 主要是 ClickHouse 导出 |
| **文件传输** | 30-120分钟 | 取决于数据量和传输方式 |
| **新机器准备** | 15-30分钟 | 安装 Docker 和 PostgreSQL |
| **数据恢复** | 20-40分钟 | Docker 拉取镜像 + 数据导入 |
| **验证测试** | 10-20分钟 | 确保所有服务正常 |
| **总计** | **2-4小时** | 大部分是等待时间 |

---

## 🎉 成功标志

迁移成功后你会看到：

```bash
# 1. 所有 Docker 服务运行
$ docker-compose -f infra/compose.yml ps
NAME                   STATUS
trading_clickhouse     Up
trading_kafka          Up
trading_redis          Up
trading_grafana        Up
...

# 2. PostgreSQL 连接正常
$ python scripts/test_postgres_connection.py
✓ PostgreSQL 连接成功
✓ 数据库结构完整
Schema 'core': 32 张表

# 3. Grafana 可访问
http://localhost:3000 显示所有 Dashboard

# 4. ClickHouse 数据完整
$ docker exec trading_clickhouse clickhouse-client --query "
    SELECT count() FROM trading.bars_1m_ibkr
"
（显示你的数据行数）

# 5. Python 应用运行
$ source venv/bin/activate
$ python main.py
（应用正常启动）
```

---

## 📝 文档版本

```
版本: 1.0
创建日期: 2025-10-15
针对环境: macOS (Homebrew) → 新机器 (Docker + 本地 PostgreSQL)
Python 版本: 3.13
PostgreSQL 版本: 15.14
```

---

## 🏆 推荐阅读顺序

### 第一次迁移？

```
1. README_MIGRATION.md（15分钟）
   ↓ 了解整体方案

2. SERVICES_SETUP.md（10分钟）
   ↓ 理解架构设计

3. CONFIG_SYNC.md（5分钟）
   ↓ 确认配置一致

4. 执行备份脚本
   bash scripts/backup_for_migration.sh

5. 在新机器执行恢复
   bash scripts/restore_on_new_machine.sh

6. 验证和测试
```

### 深入了解？

```
1. DOCKER_VS_LOCAL.md
   → 理解为什么选择 Docker

2. MIGRATION_GUIDE.md
   → 了解详细步骤和原理

3. POSTGRES_MIGRATION.md
   → PostgreSQL 专项知识
```

---

**祝迁移顺利！** 🚀

有任何问题，先查看对应文档的"故障排除"部分。
