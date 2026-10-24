# PostgreSQL 独立迁移指南

## 概述

PostgreSQL 使用**本地安装**（不是 Docker），需要单独迁移。

## 为什么 PostgreSQL 用本地安装？

```yaml
优势:
  ✅ 你已经安装并配置好
  ✅ 性能更好（无虚拟化开销）
  ✅ 管理工具更方便（pgAdmin, psql等）
  ✅ 与其他本地工具集成更好

适用场景:
  - 开发环境已有 PostgreSQL
  - 需要频繁使用 PostgreSQL 工具
  - 对性能要求高
```

## 快速迁移

### 当前机器 - 备份

```bash
cd ~/Documents/trading

# 方式1: 使用自动脚本（推荐）
bash scripts/backup_postgres.sh

# 方式2: 手动备份
pg_dump -U postgres test > ~/postgres_backup.sql
```

### 新机器 - 恢复

```bash
# 1. 确保新机器已安装 PostgreSQL
# macOS:
brew install postgresql@16
brew services start postgresql@16

# Linux:
sudo apt-get install postgresql-16
sudo systemctl start postgresql

# 2. 恢复数据
cd ~/postgres_backup_YYYYMMDD_HHMMSS
bash restore_postgres.sh

# 或手动恢复:
createdb -U postgres test
pg_restore -U postgres -d test backup.dump
```

## 你的数据库统计

根据扫描结果：

```
数据库: test
Schema: core
表数量: 32 张
总大小: ~1.8 MB

主要表:
  - trial_params          (256 KB)  - Optuna 超参数优化
  - trial_user_attributes (136 KB)  - 试验配置
  - core_order_reservation (96 KB)  - 订单预留
  - symbol_config          (88 KB)  - 交易品种配置
  - trials                 (72 KB)  - 优化试验记录
  - shadow_orders          (72 KB)  - 影子订单
  - core_strategy          (64 KB)  - 策略配置
  - core_order             (48 KB)  - 订单记录
  - core_risk_event        (48 KB)  - 风控事件
  ... 等 23 张其他表
```

**重要性**: 🔴 关键
- 包含交易订单、策略配置、风控事件
- 包含 Optuna HPO 试验历史
- **必须迁移！**

## 详细步骤

### 1. 备份脚本功能

`scripts/backup_postgres.sh` 会：

1. ✅ 检查 PostgreSQL 连接
2. ✅ 统计数据库大小和表信息
3. ✅ 导出三种格式：
   - `schema.sql` - 仅结构（用于查看）
   - `full_backup.sql` - 完整 SQL（可读）
   - `backup.dump` - 压缩格式（推荐，最快）
4. ✅ 生成恢复脚本和说明文档

### 2. 恢复方式对比

| 方式 | 文件 | 速度 | 优点 | 缺点 |
|------|------|------|------|------|
| **pg_restore** | backup.dump | ⭐⭐⭐ 最快 | 压缩、并行恢复 | 二进制不可读 |
| **psql** | full_backup.sql | ⭐⭐ 中等 | 可读可编辑 | 文件较大 |

**推荐**: 使用 `backup.dump` + `pg_restore`

### 3. 恢复命令

#### 自动恢复（推荐）
```bash
cd postgres_backup_YYYYMMDD_HHMMSS
bash restore_postgres.sh
# 交互式引导，自动检测和恢复
```

#### 手动恢复
```bash
# 方法1: 压缩格式（推荐）
pg_restore -U postgres -d test \
  --clean --if-exists \
  backup.dump

# 方法2: SQL 文件
psql -U postgres -d test \
  -f full_backup.sql

# 方法3: 如果数据库不存在
createdb -U postgres test
pg_restore -U postgres -d test backup.dump
```

## 配置说明

### 连接配置

你的项目通过 `.env` 文件配置 PostgreSQL：

```bash
# .env
PG_HOST=localhost
PG_PORT=5432
PG_DB=test
PG_USER=postgres
PG_PASSWORD=postgres
```

### Python 连接

你的代码使用 `libs/config.py`:

```python
from libs.config import config

# PostgreSQL 配置会自动从 .env 读取
pg_config = config.pg_config
# {
#   'host': 'localhost',
#   'port': 5432,
#   'database': 'test',
#   'user': 'postgres',
#   'password': 'postgres'
# }
```

## 新机器 PostgreSQL 安装

### macOS

```bash
# 1. 安装
brew install postgresql@16

# 2. 启动
brew services start postgresql@16

# 3. 创建用户（如果需要）
createuser -s postgres

# 4. 创建数据库
createdb -U postgres test

# 5. 测试连接
psql -U postgres -d test -c "SELECT version();"
```

### Ubuntu/Debian

```bash
# 1. 安装
sudo apt-get update
sudo apt-get install postgresql-16

# 2. 启动
sudo systemctl start postgresql
sudo systemctl enable postgresql

# 3. 创建用户
sudo -u postgres createuser -s postgres
sudo -u postgres psql -c "ALTER USER postgres WITH PASSWORD 'postgres';"

# 4. 创建数据库
sudo -u postgres createdb -O postgres test

# 5. 测试连接
psql -U postgres -d test -c "SELECT version();"
```

### CentOS/RHEL

```bash
# 1. 安装 PostgreSQL 仓库
sudo yum install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-8-x86_64/pgdg-redhat-repo-latest.noarch.rpm

# 2. 安装 PostgreSQL 16
sudo yum install -y postgresql16-server

# 3. 初始化和启动
sudo /usr/pgsql-16/bin/postgresql-16-setup initdb
sudo systemctl start postgresql-16
sudo systemctl enable postgresql-16

# 4. 创建用户和数据库
sudo -u postgres createuser -s postgres
sudo -u postgres createdb -O postgres test
```

## 验证迁移

### 检查表数量和大小

```bash
psql -U postgres -d test -c "
  SELECT
    schemaname,
    COUNT(*) as table_count,
    pg_size_pretty(SUM(pg_total_relation_size(schemaname||'.'||tablename))) as size
  FROM pg_tables
  WHERE schemaname = 'core'
  GROUP BY schemaname;
"

# 期望输出:
#  schemaname | table_count | size
# ------------+-------------+--------
#  core       |          32 | 1832 kB
```

### 检查关键数据

```bash
# 1. 订单数据
psql -U postgres -d test -c "
  SELECT COUNT(*) as order_count FROM core.core_order;
"

# 2. 策略配置
psql -U postgres -d test -c "
  SELECT strategy_id, name, enabled FROM core.core_strategy;
"

# 3. Optuna 试验
psql -U postgres -d test -c "
  SELECT COUNT(*) as trial_count FROM core.trials;
"

# 4. 风控事件
psql -U postgres -d test -c "
  SELECT COUNT(*) as risk_event_count FROM core.core_risk_event;
"
```

### Python 连接测试

```python
# test_pg_connection.py
from libs.config import config
import psycopg2

try:
    conn = psycopg2.connect(**config.pg_config)
    cur = conn.cursor()

    # 测试查询
    cur.execute("SELECT COUNT(*) FROM core.core_order;")
    count = cur.fetchone()[0]

    print(f"✓ PostgreSQL 连接成功")
    print(f"✓ 订单表有 {count} 条记录")

    cur.close()
    conn.close()
except Exception as e:
    print(f"✗ 连接失败: {e}")
```

## 常见问题

### Q1: 备份需要多久？

**A**: 你的数据库只有 1.8MB，非常快：
- 备份时间：< 5 秒
- 传输时间：< 1 秒（本地复制）
- 恢复时间：< 10 秒

### Q2: 为什么用 `--no-owner --no-privileges`？

**A**: 简化迁移：
- 原因：不同机器的用户名和权限可能不同
- 效果：所有表归恢复时的当前用户
- 适用：单用户开发环境

如果需要保留原始权限：
```bash
pg_dump -U postgres test > backup.sql
# （去掉 --no-owner --no-privileges）
```

### Q3: 恢复时报错 "relation already exists"？

**A**: 正常现象，因为使用了 `--clean --if-exists`：
```
步骤:
1. --clean 尝试删除旧表
2. 如果表不存在，会报错（但不影响）
3. --if-exists 忽略不存在的对象
4. 继续创建新表
5. 导入数据

结果: 恢复成功，可以忽略这些警告
```

### Q4: 如何备份到远程机器？

**A**: 三种方式：

```bash
# 方式1: 通过管道直接传输（最快）
pg_dump -U postgres test | \
  ssh user@remote-host "psql -U postgres test"

# 方式2: 备份后传输
pg_dump -U postgres test > backup.sql
scp backup.sql user@remote-host:~/

# 方式3: 压缩传输（大数据库）
pg_dump -Fc -U postgres test | \
  ssh user@remote-host "cat > backup.dump"
```

### Q5: PostgreSQL 版本不兼容怎么办？

**A**: 遵循这个规则：

```yaml
兼容性:
  ✅ 可以: 旧版本 → 新版本 (例: PG 14 → PG 16)
  ❌ 不可: 新版本 → 旧版本 (例: PG 16 → PG 14)

解决方案:
  1. 升级目标机器的 PostgreSQL
  2. 或在原机器使用旧版本导出
  3. 使用 pg_dump 的 --compatible 选项

检查版本:
  psql -V  # 查看客户端版本
  psql -U postgres -c "SELECT version();"  # 查看服务端版本
```

## 高级选项

### 选择性备份

```bash
# 仅备份特定表
pg_dump -U postgres test \
  -t core.core_order \
  -t core.core_strategy \
  > partial_backup.sql

# 仅备份 schema
pg_dump -U postgres test \
  -t 'core.*' \
  > core_schema_backup.sql

# 排除某些表
pg_dump -U postgres test \
  --exclude-table='core.alembic_version' \
  > backup_no_alembic.sql
```

### 并行备份和恢复

```bash
# 并行备份（大数据库）
pg_dump -U postgres test \
  -Fd \
  -j 4 \
  -f backup_dir/

# 并行恢复
pg_restore -U postgres -d test \
  -j 4 \
  backup_dir/
```

### 定时备份

```bash
# 添加到 crontab
crontab -e

# 每天凌晨 2 点备份
0 2 * * * cd ~/Documents/trading && bash scripts/backup_postgres.sh

# 保留最近 7 天的备份
0 3 * * * find ~/postgres_backup_* -mtime +7 -exec rm -rf {} \;
```

## 故障排除

### 连接被拒绝

```bash
# 检查 PostgreSQL 是否运行
pg_isready -h localhost -p 5432

# 如果未运行，启动它
# macOS:
brew services start postgresql@16

# Linux:
sudo systemctl start postgresql

# 检查监听地址
sudo lsof -i :5432
```

### 权限问题

```bash
# 检查用户权限
psql -U postgres -d postgres -c "\du"

# 授予权限
psql -U postgres -c "ALTER USER postgres WITH SUPERUSER;"

# 或创建新用户
createuser -s -P postgres
```

### 密码认证失败

```bash
# 修改 pg_hba.conf 允许密码认证
# 文件位置:
# macOS: /opt/homebrew/var/postgresql@16/pg_hba.conf
# Linux: /etc/postgresql/16/main/pg_hba.conf

# 添加或修改:
local   all   all   md5
host    all   all   127.0.0.1/32   md5

# 重启 PostgreSQL
brew services restart postgresql@16  # macOS
sudo systemctl restart postgresql    # Linux
```

## 备份最佳实践

1. **定期备份**
   - 开发环境：每天或每周
   - 重要数据：每天多次
   - 使用自动化脚本

2. **多地备份**
   - 本地备份
   - 外部硬盘
   - 云存储（加密）

3. **测试恢复**
   - 定期测试恢复流程
   - 验证数据完整性
   - 记录恢复时间

4. **版本控制**
   - Schema 文件纳入 Git
   - 迁移脚本版本管理
   - 记录备份时间和版本

## 总结

### PostgreSQL 迁移检查清单

**当前机器:**
- [ ] 运行 `bash scripts/backup_postgres.sh`
- [ ] 验证备份文件（backup.dump, full_backup.sql）
- [ ] 记录数据库统计（表数量、大小）
- [ ] 复制备份目录到新机器

**新机器:**
- [ ] 安装 PostgreSQL（同版本或更高）
- [ ] 启动 PostgreSQL 服务
- [ ] 创建数据库用户和数据库
- [ ] 运行 `bash restore_postgres.sh`
- [ ] 验证表数量和数据
- [ ] 测试 Python 应用连接

### 快速命令参考

```bash
# 备份
bash scripts/backup_postgres.sh

# 恢复
bash scripts/restore_postgres.sh

# 手动备份
pg_dump -Fc -U postgres test > backup.dump

# 手动恢复
pg_restore -U postgres -d test --clean backup.dump

# 查看表
psql -U postgres -d test -c "\dt core.*"

# 查看大小
psql -U postgres -d test -c "
  SELECT pg_size_pretty(pg_database_size('test'));
"
```

---

**PostgreSQL 迁移简单明了！** 🐘

总时间: < 5 分钟（数据小）
