# 🚀 交易系统迁移指南

一站式迁移方案 - 从当前 Mac 迁移到新电脑

## 📚 文档导航

| 文档 | 用途 | 适用人群 |
|------|------|----------|
| **[MIGRATION_GUIDE.md](MIGRATION_GUIDE.md)** | 完整迁移指南 | 想了解详细步骤 |
| **[DOCKER_VS_LOCAL.md](DOCKER_VS_LOCAL.md)** | Docker vs 本地安装对比 | 想理解技术决策 |
| **本文档** | 快速开始 | 想立即执行迁移 |

## ⚡ 5分钟快速开始

### 当前机器（备份）

```bash
cd ~/Documents/trading

# 运行自动备份脚本
bash scripts/backup_for_migration.sh

# 等待完成，会生成: ~/trading_migration_YYYYMMDD_HHMMSS/
# 提示是否压缩，选择 'y' 可以节省传输时间
```

### 传输到新机器

选择一种方式：

```bash
# 方式1: 外部硬盘
cp -r ~/trading_migration_* /Volumes/YOUR_DRIVE/

# 方式2: 网络传输（需要知道新机器IP）
rsync -avz ~/trading_migration_* NEW_COMPUTER_IP:~/

# 方式3: 如果已压缩
# 上传 ~/trading_backup_*.tar.gz 到云存储或传输给新机器
```

### 新机器（恢复）

```bash
# 1. 安装 Docker Desktop
# 下载: https://www.docker.com/products/docker-desktop

# 2. （如果传输的是压缩包）解压
cd ~
tar -xzf trading_backup_*.tar.gz

# 3. 运行恢复脚本
cd trading_migration_*/
bash scripts/restore_on_new_machine.sh

# 等待完成，所有服务会自动启动！
```

## 📊 迁移内容总结

### ✅ 需要迁移

| 项目 | 大小估计 | 重要性 | 迁移方法 |
|------|----------|--------|----------|
| **ClickHouse 数据** | 1-50GB | 🔴 关键 | Native 格式 |
| **项目代码** | ~100MB | 🔴 关键 | tar.gz |
| **配置文件** | <1MB | 🔴 关键 | 直接复制 |
| PostgreSQL | 可选 | 🟡 可选 | pg_dump |

### ❌ 不需要迁移

- Kafka 消息（临时数据）
- Redis 缓存（可重建）
- 日志文件（历史日志）
- Prometheus 监控数据（可重建）
- Grafana Dashboard（已在代码中）

## 🐳 新环境架构

### 全部使用 Docker！

```yaml
理由:
  ✅ 你已有完整的 Docker Compose 配置（infra/compose.yml）
  ✅ 一键启动所有服务
  ✅ 版本一致，环境隔离
  ✅ 未来迁移更简单
```

### 服务列表

| 服务 | Docker 镜像 | 端口 | 数据持久化 |
|------|-------------|------|------------|
| ClickHouse | clickhouse/clickhouse-server | 8123, 9000 | ✅ Volume |
| PostgreSQL | postgres:16-alpine | 5432 | ✅ Volume |
| Kafka | confluentinc/cp-kafka | 9092 | ⚪ 可选 |
| Redis | redis:7-alpine | 6379 | ⚪ 可选 |
| Grafana | grafana/grafana | 3000 | ✅ 配置文件 |
| Prometheus | prom/prometheus | 9090 | ❌ 可重建 |

## 🔍 验证迁移

### 快速检查

```bash
cd ~/Documents/trading

# 1. 检查 Docker 服务
docker-compose -f infra/compose.yml ps

# 2. 验证 ClickHouse 数据
docker exec trading_clickhouse clickhouse-client --query "
  SELECT count() FROM trading.bars_1m_ibkr
"

# 3. 访问 Grafana
open http://localhost:3000
# 用户名: admin, 密码: admin

# 4. 测试 Python 应用
source venv/bin/activate
python -c "from libs.config import config; print(config.clickhouse_config)"
```

### 服务访问地址

```
ClickHouse HTTP:  http://localhost:8123
ClickHouse Native: localhost:9000
Grafana:          http://localhost:3000
Prometheus:       http://localhost:9090
Kafka:            localhost:9092
Redis:            localhost:6379
PostgreSQL:       localhost:5432
```

## 🛠️ 常用命令

### Docker 管理

```bash
# 查看所有服务状态
docker-compose -f infra/compose.yml ps

# 查看服务日志
docker-compose -f infra/compose.yml logs -f [service_name]

# 重启某个服务
docker-compose -f infra/compose.yml restart clickhouse

# 停止所有服务
docker-compose -f infra/compose.yml down

# 启动所有服务
docker-compose -f infra/compose.yml up -d
```

### ClickHouse 操作

```bash
# 进入 ClickHouse 客户端
docker exec -it trading_clickhouse clickhouse-client

# 查询示例
docker exec trading_clickhouse clickhouse-client --query "
  SELECT
    name,
    formatReadableSize(total_bytes) as size,
    total_rows
  FROM system.tables
  WHERE database = 'trading'
"

# 导出数据
docker exec trading_clickhouse clickhouse-client --query "
  SELECT * FROM trading.bars_1m_ibkr FORMAT CSV
" > export.csv
```

### Kafka 操作

```bash
# 查看 topics
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
  --from-beginning --max-messages 10
```

## 📈 迁移时间估算

| 阶段 | 时间 | 说明 |
|------|------|------|
| 备份（当前机器） | 30-60分钟 | 取决于 ClickHouse 数据量 |
| 传输 | 30-120分钟 | 取决于传输方式和网络速度 |
| 恢复（新机器） | 20-40分钟 | Docker 拉取镜像 + 数据导入 |
| 验证测试 | 10-20分钟 | 确保所有服务正常 |
| **总计** | **1.5-4小时** | 大部分时间是等待，实际操作 < 30分钟 |

## ⚠️ 注意事项

### 备份前检查

```bash
# 1. 确认 ClickHouse 数据量
/opt/homebrew/bin/clickhouse client --query "
  SELECT formatReadableSize(sum(total_bytes))
  FROM system.tables
  WHERE database = 'trading'
"

# 2. 确保有足够磁盘空间
df -h ~

# 3. 检查所有服务运行状态
ps aux | grep -E "(clickhouse|kafka|redis)"
```

### 恢复后检查

```bash
# 1. 所有容器都在运行
docker-compose -f infra/compose.yml ps
# 应该看到所有服务状态为 "Up"

# 2. 数据完整性
docker exec trading_clickhouse clickhouse-client --query "
  SELECT count() FROM trading.bars_1m_ibkr
"
# 对比原数据量

# 3. 网络连接
docker-compose -f infra/compose.yml exec clickhouse nc -zv kafka 9093
```

## 🚨 故障排除

### 问题1: Docker 服务启动失败

```bash
# 查看详细日志
docker-compose -f infra/compose.yml logs [service_name]

# 常见原因: 端口冲突
lsof -i :8123  # ClickHouse HTTP
lsof -i :9092  # Kafka

# 解决: 停止冲突服务或修改端口
```

### 问题2: ClickHouse 数据导入失败

```bash
# 检查表结构是否存在
docker exec trading_clickhouse clickhouse-client --query "
  SHOW TABLES FROM trading
"

# 手动创建表（如果需要）
docker exec -i trading_clickhouse clickhouse-client < table_schema.sql

# 重试导入
docker exec -i trading_clickhouse clickhouse-client --query "
  INSERT INTO trading.bars_1m_ibkr FORMAT Native
" < bars_1m_ibkr.native
```

### 问题3: Python 连接不上 Docker 服务

```bash
# 检查 .env 配置
cat .env | grep -E "(CH_HOST|KAFKA_BOOTSTRAP|REDIS)"

# 应该是:
# CH_HOST=localhost
# KAFKA_BOOTSTRAP_SERVERS=localhost:9092
# STATEBUS_REDIS=redis://127.0.0.1:6379/0

# 测试连接
docker exec trading_clickhouse clickhouse-client --query "SELECT 1"
docker exec trading_redis redis-cli ping
```

### 问题4: Grafana Dashboard 没有显示

```bash
# 检查配置文件
ls -la grafana/dashboards/
ls -la grafana/provisioning/datasources/

# 重启 Grafana
docker-compose -f infra/compose.yml restart grafana

# 查看日志
docker-compose -f infra/compose.yml logs grafana
```

## 📝 迁移检查清单

### 迁移前（当前机器）

- [ ] 确认 ClickHouse 数据量和重要性
- [ ] 检查磁盘空间（至少 2x 数据量）
- [ ] 确认所有配置文件已保存
- [ ] 运行 `backup_for_migration.sh`
- [ ] 验证备份完整性（检查 README.txt）
- [ ] 准备传输方式（硬盘/网络/云存储）

### 迁移中

- [ ] 备份文件安全复制到新机器
- [ ] 验证文件完整性（大小/MD5）
- [ ] 在新机器解压/展开备份

### 迁移后（新机器）

- [ ] Docker Desktop 已安装并运行
- [ ] 运行 `restore_on_new_machine.sh`
- [ ] 所有 Docker 容器状态为 "Up"
- [ ] ClickHouse 数据量一致
- [ ] Grafana 可访问
- [ ] Python 应用连接测试成功
- [ ] 运行一次完整的交易策略测试

## 🎯 成功标志

迁移成功的标志：

```bash
# 1. 所有服务运行
$ docker-compose -f infra/compose.yml ps
NAME                   STATUS
trading_clickhouse     Up
trading_kafka          Up
trading_redis          Up
trading_grafana        Up
trading_postgres       Up
...

# 2. 数据完整
$ docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM trading.bars_1m_ibkr"
1234567  # 应与原数据量一致

# 3. Grafana Dashboard 正常
访问 http://localhost:3000 可以看到所有 Dashboard

# 4. Python 应用正常
$ source venv/bin/activate
$ python test_connection.py
✓ ClickHouse 连接成功
✓ Kafka 连接成功
✓ Redis 连接成功
```

## 📞 需要帮助？

### 参考文档
- [完整迁移指南](MIGRATION_GUIDE.md) - 详细步骤和说明
- [Docker 决策指南](DOCKER_VS_LOCAL.md) - 技术选型分析

### 脚本位置
- 备份脚本: `scripts/backup_for_migration.sh`
- 恢复脚本: `scripts/restore_on_new_machine.sh`

### 有用的资源
- ClickHouse 文档: https://clickhouse.com/docs
- Docker Compose 文档: https://docs.docker.com/compose/
- Kafka 文档: https://kafka.apache.org/documentation/

## 🎉 迁移后优化

迁移完成后，你的新环境将拥有：

1. **统一的服务管理**
   - 一条命令启动/停止所有服务
   - Docker Compose 管理依赖关系

2. **更好的隔离性**
   - 服务独立运行
   - 不污染系统环境

3. **更简单的未来迁移**
   - 下次迁移只需复制备份和运行脚本
   - 配置文件都在代码中

4. **更容易的版本管理**
   - 升级服务只需修改 Docker 镜像版本
   - 回滚也很简单

5. **更强的可移植性**
   - Mac/Linux/Windows 都可以运行
   - 团队协作更容易（统一环境）

---

**祝迁移顺利！** 🚀
